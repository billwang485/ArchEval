#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mini-architect-bench — MNSIM 2.0 hardware-modeling runner.

A thin, dependency-free wrapper around MNSIM 2.0's modeling pipeline that
mirrors the hardware-modeling block of the upstream ``main.py`` but does
NOT require a trained-weights file for the default (hardware-only) path.

Upstream ``main.py`` always ``torch.load()``s a weights file at
``TrainTestInterface`` construction, which forces either the real OneDrive
weights or a shape-fragile synthetic stand-in. The analytical hardware
models (latency / area / power / energy), however, only need the NN
*structure* (built from ``SimConfig.ini``) — not trained values. So for
``--mode hw`` we construct the interface with ``weights_file=None`` and run
the four hardware models exactly as ``main.py`` does.

``--mode accuracy`` additionally loads a real weights file and runs the
accuracy simulation (needs the weights + a CIFAR-10 download) — opt-in for
a future challenge.

Output is the same MNSIM text (``Entire latency:``, ``Hardware area:``,
``Hardware power:``, ``Hardware energy:``, accuracy lines); build_and_run.sh
parses it into JSON.
"""
import argparse
import sys

from MNSIM.Interface.interface import TrainTestInterface
from MNSIM.Mapping_Model.Tile_connection_graph import TCG
from MNSIM.Latency_Model.Model_latency import Model_latency
from MNSIM.Area_Model.Model_Area import Model_area
from MNSIM.Power_Model.Model_inference_power import Model_inference_power
from MNSIM.Energy_Model.Model_energy import Model_energy


def main():
    parser = argparse.ArgumentParser(description="MNSIM 2.0 hardware-modeling runner")
    parser.add_argument("-HWdes", "--hardware_description", required=True,
                        help="Hardware description file (SimConfig.ini) path")
    parser.add_argument("-NN", "--NN", default="vgg8",
                        help="NN model name (default: vgg8)")
    parser.add_argument("--mode", choices=["hw", "accuracy"], default="hw",
                        help="hw = latency/area/power/energy only (default, "
                             "no weights needed); accuracy = also run the "
                             "accuracy simulation (needs --weights + dataset)")
    parser.add_argument("-Weights", "--weights", default=None,
                        help="NN weights .pth (required only for --mode accuracy)")
    parser.add_argument("-D", "--device", default=None,
                        help="GPU device id; default CPU. MNSIM falls back to "
                             "CPU when CUDA is unavailable regardless.")
    args = parser.parse_args()

    hw = args.hardware_description
    weights = args.weights if args.mode == "accuracy" else None

    interface = TrainTestInterface(
        network_module=args.NN,
        dataset_module="MNSIM.Interface.cifar10",
        SimConfig_path=hw,
        weights_file=weights,
        device=args.device,
    )

    structure_file = interface.get_structure()
    tcg_mapping = TCG(structure_file, hw)

    # ---- hardware models (mirror main.py) ----
    latency = Model_latency(NetStruct=structure_file, SimConfig_path=hw,
                            TCG_mapping=tcg_mapping)
    latency.calculate_model_latency(mode=1)
    print("========================Latency Results=================================")
    latency.model_latency_output(1, 1)

    area = Model_area(NetStruct=structure_file, SimConfig_path=hw,
                      TCG_mapping=tcg_mapping)
    print("========================Area Results=================================")
    area.model_area_output(1, 1)

    power = Model_inference_power(NetStruct=structure_file, SimConfig_path=hw,
                                  TCG_mapping=tcg_mapping)
    print("========================Power Results=================================")
    power.model_power_output(1, 1)

    energy = Model_energy(NetStruct=structure_file, SimConfig_path=hw,
                          TCG_mapping=tcg_mapping, model_latency=latency,
                          model_power=power)
    print("========================Energy Results=================================")
    energy.model_energy_output(1, 1)

    # ---- optional accuracy simulation ----
    if args.mode == "accuracy":
        from MNSIM.Accuracy_Model.Weight_update import weight_update
        print("======================================")
        print("Accuracy simulation will take a few minutes on CPU")
        weight = interface.get_net_bits()
        weight_2 = weight_update(hw, weight, is_Variation=False,
                                 is_SAF=True, is_Rratio=False)
        print("Original accuracy:",
              interface.origin_evaluate(method="FIX_TRAIN", adc_action="SCALE"))
        print("PIM-based computing accuracy:",
              interface.set_net_bits_evaluate(weight_2, adc_action="SCALE"))


if __name__ == "__main__":
    sys.exit(main())
