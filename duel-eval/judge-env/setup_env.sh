#!/bin/bash

# Stop the script on any error
set -e

# Attempt to find Conda's base directory and source it (required for `conda activate`)
CONDA_BASE=$(conda info --base)

if [ -z "${CONDA_BASE}" ]; then
    echo "Conda is not installed or not in the PATH"
    exit 1
fi

PATH="${CONDA_BASE}/bin/":$PATH
source "${CONDA_BASE}/etc/profile.d/conda.sh"

# Create conda environment and activate it
conda env create -f conda_env.yml
conda activate judge-env
conda info --env