#!/bin/bash

# Exit on any error
set -e

# Determine the active Conda environment.
active_env=$(conda info --envs | grep '*' | awk '{print $1}')

# Deactivate if not in base
if [ "$active_env" == "base" ]; then
  echo "Active environment is 'base'; skipping deactivate."
else
  CONDA_BASE=$(conda info --base)
  source "${CONDA_BASE}/etc/profile.d/conda.sh"
  conda deactivate
fi

# Remove the judge-env environment.
conda env remove -y --name judge-env

