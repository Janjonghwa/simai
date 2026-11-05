set -e
export PATH=$HOME/.local/bin:$PATH
eval "$(micromamba shell hook -s bash)"
micromamba activate grt
export CUDA_HOME=$CONDA_PREFIX
cd /mnt/c/Users/SSAFY/simai-local/grtmesh-poc/grtmesh
# 윈도우 마운트에서 빌드하면 느리므로 WSL 홈에 복사
rm -rf ~/grtmesh && cp -r . ~/grtmesh && cd ~/grtmesh
nvcc --version | tail -2
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DCMAKE_CUDA_ARCHITECTURES=89 \
  -DCMAKE_C_COMPILER=$CC -DCMAKE_CXX_COMPILER=$CXX -DCMAKE_CUDA_HOST_COMPILER=$CXX \
  -DCMAKE_CUDA_COMPILER=$CONDA_PREFIX/bin/nvcc -DOpenCV_DIR=$CONDA_PREFIX/lib/cmake/opencv4 2>&1 | tail -25
cmake --build build -j4 2>&1 | tail -40
ls bin* 2>/dev/null
