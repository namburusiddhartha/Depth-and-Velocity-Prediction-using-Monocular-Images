# MonoFlex
This part has implementation of Modified monoflex.

Instead of learning decoupled representation of objects (8, 22 and 89) 3D keypoints are directly regressed.


**Work in progress.**


## Installation
This repo is tested with Ubuntu 20.04, python==3.7, pytorch==1.4.0 and cuda==10.1

```bash
conda create -n monoflex python=3.7

conda activate monoflex
```

Install PyTorch and other dependencies:

```bash
conda install pytorch==1.4.0 torchvision==0.5.0 cudatoolkit=10.1 -c pytorch

pip install -r requirements.txt
```

Build DCNv2 and the project
```bash
cd models/backbone/DCNv2

. make.sh

cd ../../..

python setup develop
```

## Data Preparation
Please download [KITTI dataset](http://www.cvlibs.net/datasets/kitti/eval_object.php?obj_benchmark=3d) and organize the data as follows:

```
#ROOT		
  |training/
    |calib/
    |image_2/
    |label/
    |ImageSets/
  |testing/
    |calib/
    |image_2/
    |ImageSets/
```

Then modify the paths in config/paths_catalog.py according to your data path.

## Training & Evaluation

Training with one GPU. (TODO: The multi-GPU training will be further tested.)

```bash
CUDA_VISIBLE_DEVICES=0 python tools/plain_train_net.py --batch_size 8 --config runs/monoflex.yaml --output output/exp
```

The model will be evaluated periodically (can be adjusted in the CONFIG) during training and you can also evaluate a checkpoint with

```bash
CUDA_VISIBLE_DEVICES=0 python tools/plain_train_net.py --config runs/monoflex.yaml --ckpt YOUR_CKPT  --eval
```


## Reference:


```latex
@InProceedings{MonoFlex,
    author    = {Zhang, Yunpeng and Lu, Jiwen and Zhou, Jie},
    title     = {Objects Are Different: Flexible Monocular 3D Object Detection},
    booktitle = {Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition (CVPR)},
    month     = {June},
    year      = {2021},
    pages     = {3289-3298}
}
```
