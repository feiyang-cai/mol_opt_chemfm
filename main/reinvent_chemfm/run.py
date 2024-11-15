import os
import sys
import numpy as np
path_here = os.path.dirname(os.path.realpath(__file__))
sys.path.append(path_here)
sys.path.append('/'.join(path_here.rstrip('/').split('/')[:-2]))
from main.optimizer import BaseOptimizer
#from utils import Variable, seq_to_smiles, unique
#from model import RNN
from data_structs import Experience, SampledModel
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


class REINVENT_Optimizer(BaseOptimizer):

    def __init__(self, args=None):
        super().__init__(args)
        self.model_name = "reinvent"

    def _optimize(self, oracle, config):

        self.oracle.assign_evaluator(oracle)

        path_here = os.path.dirname(os.path.realpath(__file__))

        assert torch.cuda.is_available(), 'cuda is not available'

        device_map = "auto"

        Prior = AutoModelForCausalLM.from_pretrained("ChemFM/ChemFM-1B", device_map = device_map)
        Agent = AutoModelForCausalLM.from_pretrained("ChemFM/ChemFM-1B", device_map = device_map)

        tokenizer = AutoTokenizer.from_pretrained(
            "ChemFM/ChemFM-1B",
            padding_side="right",
            use_fast=True)

        SampledAgent = SampledModel(Agent, tokenizer)
        SampledPrior = SampledModel(Prior, tokenizer)

        # We dont need gradients with respect to Prior
        for param in SampledPrior.model.parameters():
            param.requires_grad = False

        optimizer = torch.optim.Adam(SampledAgent.model.parameters(), lr=config['learning_rate'])

        # For policy based RL, we normally train on-policy and correct for the fact that more likely actions
        # occur more often (which means the agent can get biased towards them). Using experience replay is
        # therefor not as theoretically sound as it is for value based RL, but it seems to work well.
        experience = Experience(tokenizer)

        print("Model initialized, starting training...")

        step = 0
        patience = 0

        while True:

            if len(self.oracle) > 100:
                self.sort_buffer()
                old_scores = [item[1][0] for item in list(self.mol_buffer.items())[:100]]
            else:
                old_scores = 0
            
            # Sample from Agent
            #seqs, agent_likelihood, entropy = Agent.sample(config['batch_size'])
            seqs, agent_likelihood, entropy = SampledAgent.sample(config['batch_size'])

            # Remove duplicates, ie only consider unique seqs
            unique_idxs = unique(seqs)
            seqs = seqs[unique_idxs]
            agent_likelihood = agent_likelihood[unique_idxs]
            entropy = entropy[unique_idxs]

            # Get prior likelihood and score
            prior_likelihood, _ = Prior.likelihood(Variable(seqs))
            smiles = seq_to_smiles(seqs, voc)
            score = np.array(self.oracle(smiles))

            if self.finish:
                print('max oracle hit')
                break 

            # early stopping
            if len(self.oracle) > 1000:
                self.sort_buffer()
                new_scores = [item[1][0] for item in list(self.mol_buffer.items())[:100]]
                if new_scores == old_scores:
                    patience += 1
                    if patience >= self.args.patience:
                        self.log_intermediate(finish=True)
                        print('convergence criteria met, abort ...... ')
                        break
                else:
                    patience = 0

            # Calculate augmented likelihood
            augmented_likelihood = prior_likelihood.float() + config['sigma'] * Variable(score).float()
            loss = torch.pow((augmented_likelihood - agent_likelihood), 2)

            # Experience Replay
            # First sample
            if config['experience_replay'] and len(experience)>config['experience_replay']:
                exp_seqs, exp_score, exp_prior_likelihood = experience.sample(config['experience_replay'])
                exp_agent_likelihood, exp_entropy = Agent.likelihood(exp_seqs.long())
                exp_augmented_likelihood = exp_prior_likelihood + config['sigma'] * exp_score
                exp_loss = torch.pow((Variable(exp_augmented_likelihood) - exp_agent_likelihood), 2)
                loss = torch.cat((loss, exp_loss), 0)
                agent_likelihood = torch.cat((agent_likelihood, exp_agent_likelihood), 0)

            # Then add new experience
            prior_likelihood = prior_likelihood.data.cpu().numpy()
            new_experience = zip(smiles, score, prior_likelihood)
            experience.add_experience(new_experience)

            # Calculate loss
            loss = loss.mean()

            # Add regularizer that penalizes high likelihood for the entire sequence
            loss_p = - (1 / agent_likelihood).mean()
            loss += 5 * 1e3 * loss_p

            # Calculate gradients and make an update to the network weights
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

            # Convert to numpy arrays so that we can print them
            augmented_likelihood = augmented_likelihood.data.cpu().numpy()
            agent_likelihood = agent_likelihood.data.cpu().numpy()

            step += 1

