"""
Author:
    Wonjun Oh, owj0421@naver.com
Reference:
    [1] Yen-liang L, Son Tran, et al. Category-based Subspace Attention Network (CSA-Net). CVPR, 2020.
    (https://arxiv.org/abs/1912.08967?ref=dl-staging-website.ghost.io)
"""
import wandb
import torch
import torch.nn as nn
import torch.nn.functional as F
from deepfashion.utils.dataset_utils import *
from deepfashion.models.encoder.builder import *


class CSANet(nn.Module):
    def __init__(
            self,
            embedding_dim: int = 64,
            num_category: int = 12,
            img_backbone: str = 'resnet-18'
            ):
        super().__init__()
        self.embedding_dim = embedding_dim
        self.num_category = num_category

        self.img_encoder = build_img_encoder(img_backbone, embedding_dim)

        self.mask = nn.Parameter(torch.ones((num_category, embedding_dim)))
        initrange = 0.1
        self.mask.data.uniform_(-initrange, initrange)

        self.attention = nn.Sequential(
            nn.Linear(num_category * 2, num_category * 2),
            nn.ReLU(),
            nn.Linear(num_category * 2, num_category)
            )
        
    def _one_hot(self, x):
        return F.one_hot(x, num_classes=self.num_category).to(torch.float32)
    
    def _get_embedding(self, inputs, target_category):
        # Get genreral embedding from inputs
        embed = self.img_encoder(inputs['img'])
        # Compute Attention Score
        input_category = self._one_hot(inputs['category'])
        target_category = self._one_hot(target_category)
        attention_query = torch.concat([input_category, target_category], dim=-1)
        attention = F.softmax(self.attention(attention_query), dim=-1)
        # Compute Subspace Mask via Attetion
        mask = torch.matmul(self.mask.T.unsqueeze(0), attention.unsqueeze(2)).squeeze(2)
        masked_embed = embed * mask
        masked_embed = unstack_tensors(inputs['input_mask'], masked_embed)

        return masked_embed

    def forward(self, inputs, target_category=None):
        inputs = stack_dict(inputs)
        outputs = {
                'input_mask': inputs['input_mask'],
                'img_embed': None
                }
        
        if target_category is not None:
            target_category = stack_tensors(inputs['input_mask'], target_category)
            outputs['img_embed'] = self._get_embedding(inputs, target_category)
        else: # returns embedding for all categories
            embed_list = []
            for i in range(self.num_category):
                target_category = torch.ones((inputs['img'].shape[0]), dtype=torch.long, device=inputs['category'].get_device()) * i
                embed_list.append(self._get_embedding(inputs, target_category))
            outputs['img_embed'] = torch.stack(embed_list)
        
        return outputs

    def evaluation(self, dataloader, epoch, device, use_wandb=False):
        epoch_iterator = tqdm(dataloader)

        correct = 0.
        total = 0.
        for iter, batch in enumerate(epoch_iterator, start=1):
            questions = {key: value.to(device) for key, value in batch['questions'].items()}
            candidates = {key: value.to(device) for key, value in batch['candidates'].items()}

            question_outputs = self(questions)
            candidate_outputs = self(candidates)

            ans = []
            
            for batch_i in range(len(batch)):
                dists = []
                for c_i in range(torch.sum(~candidate_outputs['input_mask'][batch_i])):
                    score = 0.
                    for q_i in range(torch.sum(~question_outputs['input_mask'][batch_i])):
                        q_category = questions['category'][batch_i][q_i]
                        c_category = candidates['category'][batch_i][c_i]

                        q = question_outputs['img_embed'][c_category][batch_i][q_i]
                        c = candidate_outputs['img_embed'][q_category][batch_i][c_i]
                        score += float(nn.PairwiseDistance(p=2)(q, c))
                    dists.append(score)
                ans.append(np.argmin(np.array(dists)))
                total += 1.

            running_correct = np.sum(np.array(ans)==0)
            running_acc = running_correct / len(ans)
            correct += running_correct
            epoch_iterator.set_description(f'[FITB] Epoch: {epoch + 1:03} | Acc: {running_acc:.5f}')
            if use_wandb:
                log = {
                    f'FITB_acc': running_acc, 
                    f'FITB_step': epoch * len(epoch_iterator) + iter
                    }
                wandb.log(log)
        # Final Log
        total_acc = correct / total
        print( f'[FITB END] Epoch: {epoch + 1:03} | Acc: {total_acc:.5f} ' + '\n')

        return total_acc

    def iteration(self, dataloader, epoch, is_train, device,
                  optimizer=None, scheduler=None, use_wandb=False):
        type_str = 'Train' if is_train else 'Valid'
        epoch_iterator = tqdm(dataloader)

        total_loss = 0.
        for iter, batch in enumerate(epoch_iterator, start=1):
            anchors = {key: value.to(device) for key, value in batch['anchors'].items()}
            positives = {key: value.to(device) for key, value in batch['positives'].items()}
            negatives = {key: value.to(device) for key, value in batch['negatives'].items()}

            anc_outputs = self(anchors)
            pos_outputs = self(positives)
            neg_outputs = self(negatives)

            running_loss = []
            for b_i in range(len(anchors['input_mask'])):
                for a_i in range(torch.sum(~anchors['input_mask'][b_i])):
                    for n_i in range(torch.sum(~negatives['input_mask'][b_i])):
                        anc_category = anchors['category'][b_i][a_i]
                        pos_category = positives['category'][b_i][0]
                        anc_embed = anc_outputs['img_embed'][pos_category][b_i][a_i]
                        pos_embed = pos_outputs['img_embed'][anc_category][b_i][0]
                        neg_embed = neg_outputs['img_embed'][anc_category][b_i][n_i]
                        running_loss.append(nn.TripletMarginLoss(margin=0.3, reduction='mean')(anc_embed, pos_embed, neg_embed))
            running_loss = torch.mean(torch.stack(running_loss))

            total_loss += running_loss.item()
            if is_train == True:
                optimizer.zero_grad()
                running_loss.backward()
                torch.nn.utils.clip_grad_norm_(self.parameters(), 5)
                optimizer.step()
                if scheduler:
                    scheduler.step()
            # Log
            epoch_iterator.set_description(f'[{type_str}] Epoch: {epoch + 1:03} | Loss: {running_loss:.5f}')
            if use_wandb:
                log = {
                    f'{type_str}_loss': running_loss, 
                    f'{type_str}_step': epoch * len(epoch_iterator) + iter
                    }
                if is_train == True:
                    log["learning_rate"] = scheduler.get_last_lr()[0]
                wandb.log(log)

        # Final Log
        total_loss = total_loss / iter
        print( f'[{type_str} END] Epoch: {epoch + 1:03} | loss: {total_loss:.5f} ' + '\n')

        return total_loss