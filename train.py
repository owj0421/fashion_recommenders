import os
import wandb
import argparse
from itertools import chain

import torch
from torch.optim import AdamW
from torch.utils.data import DataLoader

import albumentations as A
from albumentations.pytorch import ToTensorV2

from deepfashion.models.type_aware_net import TypeAwareNet
from deepfashion.models.csa_net import CSANet
from deepfashion.models.fashion_swin import FashionSwin

from deepfashion.utils.trainer import *
from deepfashion.utils.dataset import *
from deepfashion.utils.metric import MetricCalculator

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=UserWarning)

# Parser
parser = argparse.ArgumentParser(description='DeepFashion')
parser.add_argument('--model', help='Model', type=Literal['type_aware_net', 'csa_net', 'fashion_swin'], default='type-aware-net')
parser.add_argument('--loss_type', help='loss_type', type=Literal['triplet', 'outfit_ranking'], default='triplet')
parser.add_argument('--embedding_dim', help='embedding dim', type=int, default=64)
parser.add_argument('--train_batch', help='Size of Batch for Training', type=int, default=64)
parser.add_argument('--valid_batch', help='Size of Batch for Validation, Test', type=int, default=64)
parser.add_argument('--n_epochs', help='Number of epochs', type=int, default=5)
parser.add_argument('--save_every', help='Number of epochs', type=int, default=3)
parser.add_argument('--scheduler_step_size', help='Step LR', type=int, default=100)
parser.add_argument('--learning_rate', help='Learning rate', type=float, default=5e-5)
parser.add_argument('--work_dir', help='Full working directory', type=str, default='F:\Projects\DeepFashion')
parser.add_argument('--data_dir', help='Full dataset directory', type=str, default='F:\Projects\datasets\polyvore_outfits')
parser.add_argument('--wandb_api_key', default=None)
parser.add_argument('--checkpoint', default=None)
args = parser.parse_args()

# Wandb
if args.wandb_api_key:
    os.environ["WANDB_API_KEY"] = args.wandb_api_key
    os.environ["WANDB_PROJECT"] = f"deep-fashion-{args.model}"
    os.environ["WANDB_LOG_MODEL"] = "all"
    wandb.login()
    run = wandb.init()

# Setup
device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')

txt_type = 'token'

training_args = TrainingArguments(
    model=args.model,
    train_batch=args.train_batch,
    valid_batch=args.valid_batch,
    n_epochs=args.n_epochs,
    save_every=args.save_every,
    learning_rate=args.learning_rate,
    work_dir = args.work_dir,
    use_wandb = True if args.wandb_api_key else False
    )
    
train_dataset_args = DatasetArguments(
    polyvore_split = 'nondisjoint',
    task_type = args.loss_type,
    dataset_type = 'train',
    img_size = (224, 224),
    img_transform = A.Compose([
        A.Resize(224, 224),
        A.HorizontalFlip(),
        A.Normalize(),
        ToTensorV2()
        ]),
    txt_type = txt_type,
    txt_max_token = 16,
    )

valid_dataset_args = DatasetArguments(
    polyvore_split = 'nondisjoint',
    task_type = args.loss_type,
    dataset_type = 'valid',
    img_size = (224, 224),
    img_transform = A.Compose([
        A.Resize(224, 224),
        A.Normalize(),
        ToTensorV2()
        ]),
    txt_type = txt_type,
    txt_max_token = 16,
    )

fitb_dataset_args = DatasetArguments(
    polyvore_split = 'nondisjoint',
    task_type = 'fitb',
    dataset_type = 'valid',
    img_size = (224, 224),
    img_transform = A.Compose([
        A.Resize(224, 224),
        A.Normalize(),
        ToTensorV2()
        ]),
    txt_type = txt_type,
    txt_max_token = 16,
    )

tokenizer = AutoTokenizer.from_pretrained('sentence-transformers/paraphrase-albert-small-v2')

train_dataloader = DataLoader(PolyvoreDataset(args.data_dir, train_dataset_args, tokenizer),
                              training_args.train_batch, shuffle=True)
valid_dataloader = DataLoader(PolyvoreDataset(args.data_dir, valid_dataset_args, tokenizer),
                              training_args.valid_batch, shuffle=False)
fitb_dataloader = DataLoader(PolyvoreDataset(args.data_dir, fitb_dataset_args, tokenizer), 
                             training_args.valid_batch, shuffle=False)

categories = 12

if args.model == 'type-aware-net':
    model = TypeAwareNet(embedding_dim=args.embedding_dim, categories=categories).to(device)
elif args.model == 'csa-net':
    model = CSANet(embedding_dim=args.embedding_dim, categories=categories).to(device)
elif args.model == 'fashion-swin':
    model = FashionSwin(embedding_dim=args.embedding_dim, categories=categories).to(device)
print('[COMPLETE] Build Model')

optimizer = AdamW(model.parameters(), lr=training_args.learning_rate,)
scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=args.scheduler_step_size, gamma=0.5)
metric = MetricCalculator()
trainer = Trainer(training_args, model, train_dataloader, valid_dataloader, fitb_dataloader,
                  optimizer=optimizer, metric=metric , scheduler=scheduler)

if args.checkpoint != None:
    checkpoint = args.checkpoint
    trainer.load(checkpoint, load_optim=False)
    print(f'[COMPLETE] Load Model from {checkpoint}')

# Train
trainer.fit()