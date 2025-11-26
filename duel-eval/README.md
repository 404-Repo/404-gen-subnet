# Duel Evaluation Workflow

## Overview
This workflow evaluates two 3D Gaussian splatting models by rendering their outputs and running an automated visual evaluation. It first renders .ply/.splat inputs from each model into 2×2 image grids, then passes these paired grids to the GLM-4.1V-Thinking vLLM judge, which deterministically decides which of the two models is better.

## Hardware Requirements 
**Rendering** requires around **1GB VRAM**  
**Duel Eval** requires at least **24GB VRAM** (eg: RTX 4090)

## Rendering
1. Set up the renderer environment:
```bash
cd render-env
bash setup_env.sh
conda activate render-env
```
2. Render models inside folder(s):
```bash
python scripts/render_2x2_grid.py \
  --folders path_to_3d_models/folder1 path_to_3d_models/folder2 ... 
```
saves the renders (default): `./outputs/2x2_renders/folder1` `./outputs/2x2_renders/folder2` ...

## Duel Eval
1. Set up the judge environment (installs vLLM):
```bash
cd judge-env 
bash setup_env.sh
conda activate judge-env
```
2. Start the vllm server:
```bash
bash scripts/start_vllm_server.sh
```
3. Run the judge, supplying prompt folder (required):
```bash
python scripts/run_duels.py \
  --left-folder ./outputs/2x2_renders/folder1 \
  --right-folder ./outputs/2x2_renders/folder2 \
  --prompt-folder path_to_input_prompt_folder
  ```
Result analysis is printed and saved in (default): `./outputs/duel_results/folder1_vs_folder2.csv`.

