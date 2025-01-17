# Copyright (c) Dale Gaines II
# Distributed under the terms of the MIT LICENSE

import glob
import json
import logging
import os
import pkgutil
import shutil
import warnings
from datetime import time, timedelta
from functools import cached_property

import numpy as np
from pymatgen.io.vasp import Poscar, Potcar

from vasp_manager.utils import change_directory, get_pmg_structure_from_poscar, pcat

logger = logging.getLogger(__name__)


class VaspInputCreator:
    """
    Handles VASP file creation
    """

    def __init__(
        self,
        calc_path,
        mode,
        poscar_source_path,
        primitive=True,
        name=None,
        increase_nodes_by_factor=1,
        increase_walltime_by_factor=1,
        poscar_significant_figures=8,
        ncore_per_node_for_memory=0,
    ):
        """
        Args:
            calc_path
            mode
            poscar_source_path
            name
            increase_nodes_by_factor
            poscar_significant_figures
        """
        self.calc_path = calc_path
        self.poscar_source_path = poscar_source_path
        self.primitive = primitive
        self.increase_nodes_by_factor = int(increase_nodes_by_factor)
        self.increase_walltime_by_factor = int(increase_walltime_by_factor)
        self.name = name
        self.mode = self._get_mode(mode)
        self.poscar_significant_figures = poscar_significant_figures
        self.ncore_per_node_for_memory = ncore_per_node_for_memory

    def _get_mode(self, mode):
        # rlx-coarse, rlx, bulkmod, stc, or elastic
        # needed to add this to ensure bulkmod or bulkmod_standalone
        # share same config
        if "bulkmod" in mode:
            mode = "bulkmod"
        return mode

    def _get_lmaxmix(self, composition_dict):
        d_f_block = json.loads(
            pkgutil.get_data(
                "vasp_manager", os.path.join("static_files", "d_f_block.json")
            ).decode("utf-8")
        )
        d_block_elements = d_f_block["d_block"]
        f_block_elements = d_f_block["f_block"]
        lmaxmix = 2
        for element in composition_dict:
            if element in d_block_elements:
                lmaxmix = 4
        for element in composition_dict:
            if element in f_block_elements:
                lmaxmix = 6
        return lmaxmix

    @cached_property
    def calc_config_dict(self):
        # sits in calculation folder like calculations/material_name/mode
        # first pardir is material_name/
        # second pardir is calculations/
        # config dict should sit in calculations/
        all_calcs_dir = os.path.dirname(os.path.dirname(self.calc_path))
        fname = "calc_config.json"
        fpath = os.path.join(all_calcs_dir, fname)
        if os.path.exists(fpath):
            with open(fpath) as fr:
                calc_config = json.load(fr)
        else:
            raise Exception(f"No {fname} found in path {os.path.abspath(all_calcs_dir)}")
        return calc_config

    @cached_property
    def computing_config_dict(self):
        all_calcs_dir = os.path.dirname(os.path.dirname(self.calc_path))
        fname = "computing_config.json"
        fpath = os.path.join(all_calcs_dir, fname)
        if os.path.exists(fpath):
            with open(fpath) as fr:
                computing_config = json.load(fr)
        else:
            raise Exception(f"No {fname} found in path {os.path.abspath(all_calcs_dir)}")
        return computing_config

    @cached_property
    def computer(self):
        return self.computing_config_dict["computer"]

    @cached_property
    def source_structure(self):
        num_archives = len(glob.glob(os.path.join(self.calc_path, "archive*")))
        if num_archives > 0:
            archive_name = f"archive_{num_archives-1}"
            self.poscar_source_path = os.path.join(
                self.calc_path, archive_name, "CONTCAR"
            )
        try:
            structure = get_pmg_structure_from_poscar(
                self.poscar_source_path, primitive=self.primitive
            )
        except Exception as e:
            raise Exception(f"Cannot load POSCAR in {self.poscar_source_path}: {e}")
        return structure

    @cached_property
    def incar_template(self):
        incar_template = pkgutil.get_data(
            "vasp_manager", os.path.join("static_files", "INCAR_template")
        ).decode("utf-8")
        return incar_template

    @cached_property
    def potcar_dict(self):
        potcar_dict = json.loads(
            pkgutil.get_data(
                "vasp_manager", os.path.join("static_files", "pot_dict.json")
            ).decode("utf-8")
        )
        return potcar_dict

    @cached_property
    def q_mapper(self):
        q_mapper = json.loads(
            pkgutil.get_data(
                "vasp_manager", os.path.join("static_files", "q_handles.json")
            ).decode("utf-8")
        )
        return q_mapper

    def make_poscar(self):
        """
        Create and write a POSCAR
        """
        poscar = Poscar(self.source_structure)
        poscar_path = os.path.join(self.calc_path, "POSCAR")
        poscar.write_file(
            poscar_path, significant_figures=self.poscar_significant_figures
        )

    @property
    def n_nodes(self):
        # start with 1 node per 32 atoms
        num_nodes = (len(self.source_structure) // 32) + 1
        if self.computer == "quest":
            # quest has 4x smaller nodes than perlmutter
            num_nodes *= 4
        num_nodes *= self.increase_nodes_by_factor
        return num_nodes

    @property
    def n_procs(self):
        # typically request all processors on each node, and then
        # leave some ~4/node empty for memory
        n_procs = (
            self.n_nodes * self.computing_config_dict[self.computer]["ncore_per_node"]
        )
        return n_procs

    @property
    def n_procs_used(self):
        ncore_per_node = self.computing_config_dict[self.computer]["ncore_per_node"]
        if self.mode == "elastic":
            if self.computer == "quest":
                self.ncore_per_node_for_memory += 8
        return self.n_nodes * (ncore_per_node - self.ncore_per_node_for_memory)

    def make_potcar(self):
        """
        Create and write a POTCAR
        """
        potcar_path = os.path.join(self.calc_path, "POTCAR")
        potcar_dir = self.computing_config_dict[self.computer]["potcar_dir"]

        el_names = [el.name for el in self.source_structure.composition]
        logger.debug(f"{self.source_structure.composition.reduced_formula}, {el_names}")
        pot_singles = [
            os.path.join(potcar_dir, self.potcar_dict[el_name], "POTCAR")
            for el_name in el_names
        ]
        for pot_single in pot_singles:
            if not os.path.exists(pot_single):
                msg = "Unable to create POTCAR"
                msg += f"\n\t POTCAR not found at path {pot_single}"
                raise Exception(msg)

        potcar = pcat(pot_singles)
        with open(potcar_path, "w+") as fw:
            fw.write(potcar)

    def make_incar(self):
        """
        Create and write an INCAR

        Need to modify this to account for spin/magmom
        Current kpoints coming from the kspacing tag in the INCAR,
            but future versions should include ability to make kpoints from kppra
        """
        incar_path = os.path.join(self.calc_path, "INCAR")
        ncore = self.computing_config_dict[self.computer]["ncore"]
        calc_config = self.calc_config_dict[self.mode]

        if calc_config["ispin"] != 1:
            raise NotImplementedError("ISPIN = 2 not yet supported")

        if calc_config["iopt"] != 0 and calc_config["potim"] != 0:
            raise RuntimeError("To use IOPT != 0, POTIM must be set to 0")

        composition_dict = self.source_structure.composition.as_dict()
        # read POTCAR
        potcar_path = os.path.join(self.calc_path, "POTCAR")
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=UserWarning)
            potcar = Potcar.from_file(potcar_path)
        n_electrons = 0
        for potcar_single in potcar:
            n_electrons += (
                potcar_single.nelectrons * composition_dict[potcar_single.element]
            )
        # make n_bands divisible by NCORE (VASP INCAR tag)
        calc_config["ncore"] = ncore
        calc_config["nbands"] = int(np.ceil(0.75 * n_electrons / ncore) * ncore)
        lmaxmix = self._get_lmaxmix(composition_dict)

        # Add lines to the vaspq file
        incar_tmp = self.incar_template.split("\n")
        for i, line in enumerate(incar_tmp):
            # add extra flags for elastic mode
            if "LASPH" in line:
                lmaxmix_line = f"LMAXMIX = {lmaxmix}"
                incar_tmp.insert(i + 1, lmaxmix_line)
            if self.mode == "elastic":
                if "KSPACING" in line:
                    nfree_line = "NFREE = {nfree}"
                    symprec_line = "SYMPREC = {symprec}"
                    incar_tmp.insert(i + 1, symprec_line)
                    incar_tmp.insert(i + 1, nfree_line)
                if "NCORE" in line:
                    # elastic calculation won't run unless NCORE=1
                    incar_tmp[i] = "NCORE = 1"
        incar_tmp = "\n".join([line for line in incar_tmp])
        incar = incar_tmp.format(**calc_config)
        logger.debug(incar)
        with open(incar_path, "w+") as fw:
            fw.write(incar)

    def make_vaspq(self):
        """
        Create and write vasp.q file
        """
        vaspq_path = os.path.join(self.calc_path, "vasp.q")
        calc_config = self.calc_config_dict[self.mode]

        # create pad string for job naming to differentiate in the queue
        match self.mode:
            case "rlx-coarse" | "rlx":
                if self.mode == "rlx":
                    pad_string = "r"
                elif self.mode == "rlx-coarse":
                    pad_string = "rc"
                mode = "rlx"
            case "static":
                pad_string = "s"
                mode = "static"
            case "bulkmod":
                pad_string = "b"
                mode = "bulkmod"
            case "elastic":
                pad_string = "e"
                mode = "elastic"
            case _:
                raise ValueError(
                    "Calculation type {self.mode} not in supported calculation types"
                    "of VaspInputCreator"
                )

        if self.name is None:
            jobname = pad_string + self.source_structure.composition.reduced_formula
        else:
            jobname = pad_string + self.name

        # convert walltime into seconds for increase_walltime_by_factor
        walltime_iso = time.fromisoformat(calc_config["walltime"])
        # walltime_duration is in seconds
        walltime_duration = timedelta(
            hours=walltime_iso.hour,
            minutes=walltime_iso.minute,
            seconds=walltime_iso.second,
        )
        walltime_duration *= self.increase_walltime_by_factor
        # convert to HH:MM:SS
        walltime = str(walltime_duration)
        # cut walltime short by 1 minute so job metrics log properly
        timeout = walltime_duration.seconds - 60
        # quest uses mpirun which needs timeout in seconds
        # otherwise, convert it back to HH:MM:SS
        if not self.computer == "quest":
            timeout = str(timedelta(seconds=timeout))

        computer_config = self.computing_config_dict[self.computer].copy()
        ncore_per_node = self.n_procs_used // self.n_nodes
        computer_config.update(
            {
                "n_nodes": self.n_nodes,
                "n_procs": self.n_procs,
                "ncore_per_node": ncore_per_node,
                "jobname": jobname,
                "walltime": walltime,
                "timeout": timeout,
            }
        )

        q_name = self.q_mapper[self.computer][mode]
        vaspq_tmp = pkgutil.get_data(
            "vasp_manager", os.path.join("static_files", q_name)
        ).decode("utf-8")
        vaspq = vaspq_tmp.format(**computer_config)
        logger.debug(vaspq)
        with open(vaspq_path, "w+") as fw:
            fw.write(vaspq)

    def make_archive_and_repopulate(self):
        """
        Make an archive of a VASP calculation and copy back over relevant files
        """
        with change_directory(self.calc_path):
            contcar_path = "CONTCAR"
            contcar_exists = os.path.exists(contcar_path)
            if contcar_exists:
                contcar_is_empty = os.stat(contcar_path).st_size == 0
            else:
                contcar_is_empty = True

            if contcar_is_empty:
                # if CONTCAR is empty, don't make an archive and clean up
                all_files = [d for d in glob.glob("*") if os.path.isfile(d)]
                for f in all_files:
                    os.remove(f)
            else:
                # make the archive
                num_previous_archives = len(glob.glob("archive*"))
                archive_name = f"archive_{num_previous_archives}"
                logger.info(f"Making {archive_name}...")
                os.mkdir(archive_name)

                all_files = [d for d in glob.glob("*") if os.path.isfile(d)]
                for f in all_files:
                    shutil.move(f, archive_name)

        self.create()

    def create(self):
        """
        Make VASP input files

        Don't touch the order! make_incar and make_vaspq rely on the poscar and
        potcar existing already
        """
        if not os.path.exists(self.calc_path):
            os.mkdir(self.calc_path)
        self.make_poscar()
        self.make_potcar()
        self.make_incar()
        self.make_vaspq()
