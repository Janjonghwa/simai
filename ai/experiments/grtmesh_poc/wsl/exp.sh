# usage: exp.sh <tag> <voxel> [extra flags]
tag=$1; vox=$2; shift 2
export PATH=$HOME/.local/bin:$PATH
eval "$(micromamba shell hook -s bash)"; micromamba activate grt
export LD_LIBRARY_PATH=$CONDA_PREFIX/lib:/usr/lib/wsl/lib:$LD_LIBRARY_PATH
cd ~/grtmesh
IN=/mnt/c/Users/SSAFY/simai-local/grtmesh-poc/input
OUT=/mnt/c/Users/SSAFY/simai-local/grtmesh-poc/out-$tag
rm -rf $OUT
( while true; do nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits; sleep 0.2; done > /tmp/gpumem-$tag.txt ) &
SP=$!
/usr/bin/time -v ./bin/multiview_rgbd $IN $OUT --voxel-size $vox "$@" > /tmp/run-$tag.log 2>&1
kill $SP
tail -25 /tmp/run-$tag.log | grep -E "Elapsed|Maximum resident|ms|time|face|vert" | head -20
sort -n /tmp/gpumem-$tag.txt | tail -1 | sed 's/^/GPU max MiB: /'; sort -n /tmp/gpumem-$tag.txt | head -1 | sed 's/^/GPU min MiB: /'
cp /tmp/run-$tag.log $OUT/run.log; ls -la $OUT $OUT/mesh
