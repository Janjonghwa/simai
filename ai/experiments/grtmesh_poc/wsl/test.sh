export PATH=$HOME/.local/bin:$PATH
eval "$(micromamba shell hook -s bash)"; micromamba activate grt
export LD_LIBRARY_PATH=$CONDA_PREFIX/lib:/usr/lib/wsl/lib:$LD_LIBRARY_PATH
cd ~/grtmesh
cmake -S . -B build -DGRTMESH_BUILD_TESTS=ON >/dev/null && cmake --build build --target grtmesh_regression 2>&1 | tail -2
./bin/grtmesh_regression 2>&1 | tail -15
