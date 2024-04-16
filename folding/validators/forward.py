import os
import time
import torch
import pickle
import bittensor as bt
from pathlib import Path
from typing import List, Dict

from folding.validators.protein import Protein
from folding.utils.uids import get_random_uids
from folding.utils.logging import log_event
from folding.validators.reward import get_rewards
from folding.protocol import FoldingSynapse
from folding.rewards.reward import RewardEvent

from folding.utils.ops import select_random_pdb_id, load_pdb_ids, get_response_info
from folding.validators.hyperparameters import HyperParameters

ROOT_DIR = Path(__file__).resolve().parents[2]
PDB_IDS = load_pdb_ids(root_dir=ROOT_DIR, filename="pdb_ids.pkl")


def save_to_pickle(data, filename):
    with open(filename, "wb") as f:
        pickle.dump(data, f)


async def run_step(
    self,
    protein: Protein,
    k: int,
    timeout: float,
    exclude: list = [],
):
    bt.logging.debug("run_step")
    start_time = time.time()

    # Get the list of uids to query for this step.
    # uids = get_random_uids(self, k=k, exclude=exclude).to(self.device)
    uids = [9, 10]
    # uids = [4,5]
    axons = [self.metagraph.axons[uid] for uid in uids]
    synapse = FoldingSynapse(pdb_id=protein.pdb_id, md_inputs=protein.md_inputs)

    # Make calls to the network with the prompt.
    responses: List[FoldingSynapse] = await self.dendrite(
        axons=axons,
        synapse=synapse,
        timeout=timeout,
        deserialize=True,  # decodes the bytestream response inside of md_outputs.
    )

    reward_events: List[RewardEvent] = get_rewards(
        protein=protein, responses=responses, uids=uids
    )

    # TODO: Extend this over all RewardEvents
    # Right now, we are only using a single energy reward model.
    reward_event = reward_events[0]
    self.update_scores(
        rewards=torch.FloatTensor(list(reward_event.rewards.values())),
        uids=list(reward_event.rewards.keys()),
    )

    response_info = get_response_info(responses=responses)

    # # Log the step event.
    event = {
        "block": self.block,
        "step_length": time.time() - start_time,
        "uids": uids,
        **response_info,
        **reward_event.asdict(),  # contains another copy of the uids used for the reward stack
    }

    bt.logging.warning(f"Event information: {event}")
    return event


def parse_config(config) -> List[str]:
    """
    Parse config to check if key hyperparameters are set.
    If they are, exclude them from hyperparameter search.
    """
    ff = config.ff
    water = config.water
    box = config.box
    exclude_in_hp_search = []

    if ff is not None:
        exclude_in_hp_search.append("FF")
    if water is not None:
        exclude_in_hp_search.append("WATER")
    if box is not None:
        exclude_in_hp_search.append("BOX")

    return exclude_in_hp_search


async def forward(self):
    bt.logging.info(f"Running config: {self.config}")

    while True:
        forward_start_time = time.time()
        exclude_in_hp_search = parse_config(self.config)

        # We need to select a random pdb_id outside of the protein class.
        pdb_id = (
            select_random_pdb_id(PDB_IDS=PDB_IDS)
            if self.config.protein.pdb_id is None
            else self.config.protein.pdb_id
        )
        hp_sampler = HyperParameters(exclude=exclude_in_hp_search)

        for iteration_num in range(hp_sampler.TOTAL_COMBINATIONS):
            hp_sampler_time = time.time()

            event = {}
            try:
                sampled_combination: Dict = hp_sampler.sample_hyperparameters()
                bt.logging.info(
                    f"pdb_id: {pdb_id}, Selected hyperparameters: {sampled_combination}, iteration {iteration_num}"
                )

                protein = Protein(
                    pdb_id=self.config.protein.pdb_id,
                    ff=self.config.protein.ff
                    if self.config.protein.ff is not None
                    else sampled_combination["FF"],
                    water=self.config.protein.water
                    if self.config.protein.water is not None
                    else sampled_combination["WATER"],
                    box=self.config.protein.box
                    if self.config.protein.box is not None
                    else sampled_combination["BOX"],
                    config=self.config.protein,
                )

                hps = {
                    "FF": protein.ff,
                    "WATER": protein.water,
                    "BOX": protein.box,
                    "BOX_DISTANCE": sampled_combination["BOX_DISTANCE"],
                }

                bt.logging.info(f"Attempting to generate challenge: {protein}")
                protein.forward()

            except Exception as E:
                bt.logging.error(
                    f"❌❌ Error running hyperparameters {sampled_combination} for pdb_id {pdb_id} ❌❌"
                )
                bt.logging.warning(E)
                event["validator_search_status"] = False

            finally:
                event["pdb_id"] = pdb_id
                event.update(hps)  # add the dictionary of hyperparameters to the event
                event["hp_sample_time"] = time.time() - hp_sampler_time
                event["forward_time"] = (
                    time.time() - forward_start_time
                )  # forward time if validator step fails

                if "validator_search_status" not in event:
                    bt.logging.info("✅✅ Simulation ran successfully! ✅✅")
                    event["validator_search_status"] = True  # simulation passed!
                    break  # break out of the loop if the simulation was successful

                log_event(
                    self, event
                )  # only log the event if the simulation was not successful

        # If we exit the for loop without breaking, it means all hyperparameter combinations failed.
        if event["validator_search_status"] is False:
            bt.logging.error(
                f"❌❌ All hyperparameter combinations failed for pdb_id {pdb_id}.. Skipping! ❌❌"
            )
            continue  # Skip to the next pdb_id

        # The following code only runs if we have a successful run!
        bt.logging.info("⏰ Waiting for miner responses ⏰")
        miner_event = await run_step(
            self,
            protein=protein,
            k=self.config.neuron.sample_size,
            timeout=self.config.neuron.timeout,
        )
        bt.logging.success("✅ All miners complete! ✅")

        event.update(miner_event)
        event["forward_time"] = time.time() - forward_start_time

        bt.logging.success("✅ Logging pdb results to wandb ✅")
        log_event(self, event)  # Log the entire pipeline.
