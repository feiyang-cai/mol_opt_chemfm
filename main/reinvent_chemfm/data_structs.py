import numpy as np
import random
import re
import pickle
from rdkit import Chem
import sys
import time
import torch
from torch.utils.data import Dataset

from utils import Variable
import torch.nn.functional as F

class Experience(object):
    """Class for prioritized experience replay that remembers the highest scored sequences
       seen and samples from them with probabilities relative to their scores."""
    def __init__(self, tokenizer, max_size=100):
        self.memory = []
        self.max_size = max_size
        self.tokenizer = tokenizer

    def add_experience(self, experience):
        """Experience should be a list of (smiles, score, prior likelihood) tuples"""
        self.memory.extend(experience)
        if len(self.memory)>self.max_size:
            # Remove duplicates
            idxs, smiles = [], []
            for i, exp in enumerate(self.memory):
                if exp[0] not in smiles:
                    idxs.append(i)
                    smiles.append(exp[0])
            self.memory = [self.memory[idx] for idx in idxs]
            # Retain highest scores
            self.memory.sort(key = lambda x: x[1], reverse=True)
            self.memory = self.memory[:self.max_size]
            # print("\nBest score in memory: {:.2f}".format(self.memory[0][1]))

    def sample(self, n):
        """Sample a batch size n of experience"""
        if len(self.memory)<n:
            raise IndexError('Size of memory ({}) is less than requested sample ({})'.format(len(self), n))
        else:
            scores = [x[1]+1e-10 for x in self.memory]
            sample = np.random.choice(len(self), size=n, replace=False, p=scores/np.sum(scores))
            sample = [self.memory[i] for i in sample]
            smiles = [x[0] for x in sample]
            scores = [x[1] for x in sample]
            prior_likelihood = [x[2] for x in sample]
        print(smiles)
        assert 1==2
        tokenized = [self.voc.tokenize(smile) for smile in smiles]
        encoded = [Variable(self.voc.encode(tokenized_i)) for tokenized_i in tokenized]
        encoded = MolData.collate_fn(encoded)
        return encoded, np.array(scores), np.array(prior_likelihood)

    def initiate_from_file(self, fname, scoring_function, Prior):
        """Adds experience from a file with SMILES
           Needs a scoring function and an RNN to score the sequences.
           Using this feature means that the learning can be very biased
           and is typically advised against."""
        with open(fname, 'r') as f:
            smiles = []
            for line in f:
                smile = line.split()[0]
                if Chem.MolFromSmiles(smile):
                    smiles.append(smile)
        scores = scoring_function(smiles)
        tokenized = [self.voc.tokenize(smile) for smile in smiles]
        encoded = [Variable(self.voc.encode(tokenized_i)) for tokenized_i in tokenized]
        encoded = MolData.collate_fn(encoded)
        prior_likelihood, _ = Prior.likelihood(encoded.long())
        prior_likelihood = prior_likelihood.data.cpu().numpy()
        new_experience = zip(smiles, scores, prior_likelihood)
        self.add_experience(new_experience)

    def print_memory(self, path):
        """Prints the memory."""
        print("\n" + "*" * 80 + "\n")
        print("         Best recorded SMILES: \n")
        print("Score     Prior log P     SMILES\n")
        with open(path, 'w') as f:
            f.write("SMILES Score PriorLogP\n")
            for i, exp in enumerate(self.memory[:100]):
                if i < 50:
                    print("{:4.2f}   {:6.2f}        {}".format(exp[1], exp[2], exp[0]))
                    f.write("{} {:4.2f} {:6.2f}\n".format(*exp))
        print("\n" + "*" * 80 + "\n")

    def __len__(self):
        return len(self.memory)
    
class SampledModel():
    def __init__(self, model, tokenizer):
        self.model = model
        self.tokenizer = tokenizer
        """
        with open("./", 'r') as file:
            data = json.load(file)
        # Calculate total frequency
        total_frequency = sum(data.values())
        # Calculate probabilities
        start_token_probs = {key: value / total_frequency for key, value in data.items()}
        """
        self.start_token_probs = {"C": 1.0}

    def sample(self, batch_size, max_length=140):
        start_token = Variable(torch.zeros(batch_size).long())
        strings = list(self.start_token_probs.keys())
        probs = list(self.start_token_probs.values())
        prompts = random.choices(strings, probs, k=batch_size)
        sampled_start_token = Variable(np.array(self.tokenizer(prompts)['input_ids'], dtype=np.int64)).long().squeeze(1)
        start_token[:] = sampled_start_token

        # initialize the input tensor
        input_tensor = start_token.unsqueeze(1)

        finished = torch.zeros(batch_size).byte().cuda()

        with torch.no_grad():
            for _ in range(max_length):
                logits = self.model(input_tensor)['logits'][:, -1, :]

                prob = F.softmax(logits, dim=1)
                log_prob = F.log_softmax(logits, dim=1)
                x = torch.multinomial(prob, num_samples=1).view(-1)

                input_tensor = torch.cat((input_tensor, x.view(-1, 1)), 1)
                eos_sampled = (x == self.tokenizer.eos_token_id).data
                finished = torch.ge(finished + eos_sampled, 1)
                if torch.prod(finished) == 1:
                    break

                print(x.shape, input_tensor.shape)

        assert 1==2


    #return seqs, log_probs, entropy

def replace_halogen(string):
    """Regex to replace Br and Cl with single letters"""
    br = re.compile('Br')
    cl = re.compile('Cl')
    string = br.sub('R', string)
    string = cl.sub('L', string)

    return string

def tokenize(smiles):
    """Takes a SMILES string and returns a list of tokens.
    This will swap 'Cl' and 'Br' to 'L' and 'R' and treat
    '[xx]' as one token."""
    regex = '(\[[^\[\]]{1,6}\])'
    smiles = replace_halogen(smiles)
    char_list = re.split(regex, smiles)
    tokenized = []
    for char in char_list:
        if char.startswith('['):
            tokenized.append(char)
        else:
            chars = [unit for unit in char]
            [tokenized.append(unit) for unit in chars]
    tokenized.append('EOS')
    return tokenized

def canonicalize_smiles_from_file(fname):
    """Reads a SMILES file and returns a list of RDKIT SMILES"""
    with open(fname, 'r') as f:
        smiles_list = []
        for i, line in enumerate(f):
            if i % 100000 == 0:
                print("{} lines processed.".format(i))
            smiles = line.split(" ")[0]
            mol = Chem.MolFromSmiles(smiles)
            if filter_mol(mol):
                smiles_list.append(Chem.MolToSmiles(mol))
        print("{} SMILES retrieved".format(len(smiles_list)))
        return smiles_list

def filter_mol(mol, max_heavy_atoms=50, min_heavy_atoms=10, element_list=[6,7,8,9,16,17,35]):
    """Filters molecules on number of heavy atoms and atom types"""
    if mol is not None:
        num_heavy = min_heavy_atoms<mol.GetNumHeavyAtoms()<max_heavy_atoms
        elements = all([atom.GetAtomicNum() in element_list for atom in mol.GetAtoms()])
        if num_heavy and elements:
            return True
        else:
            return False

def write_smiles_to_file(smiles_list, fname):
    """Write a list of SMILES to a file."""
    with open(fname, 'w') as f:
        for smiles in smiles_list:
            f.write(smiles + "\n")

def filter_on_chars(smiles_list, chars):
    """Filters SMILES on the characters they contain.
       Used to remove SMILES containing very rare/undesirable
       characters."""
    smiles_list_valid = []
    for smiles in smiles_list:
        tokenized = tokenize(smiles)
        if all([char in chars for char in tokenized][:-1]):
            smiles_list_valid.append(smiles)
    return smiles_list_valid

def filter_file_on_chars(smiles_fname, voc_fname):
    """Filters a SMILES file using a vocabulary file.
       Only SMILES containing nothing but the characters
       in the vocabulary will be retained."""
    smiles = []
    with open(smiles_fname, 'r') as f:
        for line in f:
            smiles.append(line.split()[0])
    print(smiles[:10])
    chars = []
    with open(voc_fname, 'r') as f:
        for line in f:
            chars.append(line.split()[0])
    print(chars)
    valid_smiles = filter_on_chars(smiles, chars)
    with open(smiles_fname + "_filtered", 'w') as f:
        for smiles in valid_smiles:
            f.write(smiles + "\n")

def combine_voc_from_files(fnames):
    """Combine two vocabularies"""
    chars = set()
    for fname in fnames:
        with open(fname, 'r') as f:
            for line in f:
                chars.add(line.split()[0])
    with open("_".join(fnames) + '_combined', 'w') as f:
        for char in chars:
            f.write(char + "\n")

def construct_vocabulary(smiles_list):
    """Returns all the characters present in a SMILES file.
       Uses regex to find characters/tokens of the format '[x]'."""
    add_chars = set()
    for i, smiles in enumerate(smiles_list):
        regex = '(\[[^\[\]]{1,6}\])'
        smiles = replace_halogen(smiles)
        char_list = re.split(regex, smiles)
        for char in char_list:
            if char.startswith('['):
                add_chars.add(char)
            else:
                chars = [unit for unit in char]
                [add_chars.add(unit) for unit in chars]

    print("Number of characters: {}".format(len(add_chars)))
    with open('data/Voc', 'w') as f:
        for char in add_chars:
            f.write(char + "\n")
    return add_chars

if __name__ == "__main__":
    smiles_file = sys.argv[1]
    print("Reading smiles...")
    smiles_list = canonicalize_smiles_from_file(smiles_file)
    print("Constructing vocabulary...")
    voc_chars = construct_vocabulary(smiles_list)
    write_smiles_to_file(smiles_list, "data/mols_filtered.smi")
