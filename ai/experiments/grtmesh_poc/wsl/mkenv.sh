#!/bin/bash
export PATH=$HOME/.local/bin:$PATH
micromamba create -y -n grt -c nvidia/label/cuda-12.4.1 -c conda-forge cuda-toolkit=12.4.1 cmake=3.29 gxx_linux-64=12 gcc_linux-64=12 ninja libopencv opencv eigen nlohmann_json make > $HOME/mm.log 2>&1
echo DONE >> $HOME/mm.log
