import os
import re
import time
import tqdm
import random
import subprocess
import hashlib

import pickle
from typing import List, Dict

import bittensor as bt
from folding.protocol import FoldingSynapse

# Recommended force field-water pairs, retrieved from gromacs-2024.1/share/top
FF_WATER_PAIRS = {
    "amber03": "tip3p",  # AMBER force fields
    "amber94": "tip3p",
    "amber96": "tip3p",
    "amber99": "tip3p",
    "amber99sb-ildn": "tip3p",
    "amber99sb": "tip3p",
    "amberGS": "tip3p",
    "charmm27": "tip3p",  # CHARMM all-atom force field
    "gromos43a1": "spc",  # GROMOS force fields
    "gromos43a2": "spc",
    "gromos45a3": "spc",
    "gromos53a5": "spc",
    "gromos53a6": "spc",
    "gromos54a7": "spc",
    "oplsaa": "tip4p",  # OPLS all-atom force field
}


def load_pdb_ids(root_dir: str, filename: str = "pdb_ids.pkl") -> Dict[str, List[str]]:
    """If you want to randomly sample pdb_ids, you need to load in
    the data that was computed via the gather_pdbs.py script.

    Args:
        root_dir (str): location of the file that contains all the names of pdb_ids
        filename (str, optional): name of the pdb_id file. Defaults to "pdb_ids.pkl".
    """
    PDB_PATH = os.path.join(root_dir, filename)

    if not os.path.exists(PDB_PATH):
        raise ValueError(
            f"Required Pdb file {PDB_PATH!r} was not found. Run `python scripts/gather_pdbs.py` first."
        )

    with open(PDB_PATH, "rb") as f:
        PDB_IDS = pickle.load(f)
    return PDB_IDS


def select_random_pdb_id(PDB_IDS: Dict) -> str:
    """This function is really important as its where you select the protein you want to fold"""
    while True:
        try:
            family = random.choice(list(PDB_IDS.keys()))
            choices = PDB_IDS[family]
        except:
            choices = PDB_IDS  # There is a clase where the dictionary is just a list of values.
        finally:
            if len(choices):
                return random.choice(choices)


def gro_hash(gro_path: str):
    """Generates the hash for a specific gro file.
    Enables validators to ensure that miners are running the correct
    protein, and not generating fake data.

    Connects the (residue name, atom name, and residue number) from each line
    together into a single string. This way, we can ensure that the protein is the same.

    Example:
    10LYS  N  1
    10LYS  H1 2

    Output: 10LYSN1LYSH12

    Args:
        gro_path (str): location to the gro file
    """
    bt.logging.info(f"Calculating hash for path {gro_path!r}")
    pattern = re.compile(r"\s*(\d+\w+)\s+(\w+\d*\s*\d+)\s+(\-?\d+\.\d+)+")

    with open(gro_path, "rb") as f:
        name, length, *lines, _ = f.readlines()
        length = int(length)
        bt.logging.info(f"{name=}, {length=}, {len(lines)=}")

    buf = ""
    for line in lines:
        line = line.decode().strip()
        match = pattern.match(line)
        if not match:
            raise Exception(f"Error parsing line in {gro_path!r}: {line!r}")
        buf += match.group(1) + match.group(2).replace(" ", "")

    return hashlib.md5(name + buf.encode()).hexdigest()


def check_if_directory_exists(output_directory):
    if not os.path.exists(output_directory):
        os.makedirs(output_directory)
        bt.logging.debug(f"Created directory {output_directory!r}")


def run_cmd_commands(commands: List[str], suppress_cmd_output: bool = True):
    timings = {}
    errors = {}

    for cmd in tqdm.tqdm(commands):
        bt.logging.info(f"Running command: {cmd}")

        start_time = time.time()
        try:
            result = subprocess.run(
                cmd,
                check=True,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            timings[cmd] = time.time() - start_time

            if not suppress_cmd_output:
                bt.logging.info(result.stdout.decode())

        except subprocess.CalledProcessError as e:
            bt.logging.error(f"❌ Failed to run command ❌: {cmd}")
            if not suppress_cmd_output:
                bt.logging.error(f"Error: {e.stderr.decode()}")

            errors["breaking_command"] = e.cmd
            errors["command_output"] = e.stdout.decode()
            errors["return_code"] = e.returncode
            return timings, errors

    return timings, errors


def get_response_info(responses: List[FoldingSynapse]) -> Dict:
    """Gather all desired response information from the set of miners."""

    response_times = []
    response_status_messages = []
    response_status_codes = []
    response_returned_files = []

    for resp in responses:
        if resp.dendrite.process_time != None:
            response_times.append(resp.dendrite.process_time)
        else:
            response_times.append(0)

        response_status_messages.append(str(resp.dendrite.status_message))
        response_status_codes.append(str(resp.dendrite.status_code))
        response_returned_files.append(list(resp.md_output.keys()))

    return {
        "response_times": response_times,
        "response_status_messages": response_status_messages,
        "response_status_codes": response_status_codes,
        "response_returned_files": response_returned_files,
    }
