import json
import os
import joblib
import yaml
import numpy as np
from joblib import delayed
from rdkit import Chem
from rdkit.Chem import Draw
import tdc
from tdc.generation import MolGen
import wandb
from main.utils.chem import *


class BaseOptimizer:

    def __init__(self, args=None):
        self.model_name = "Default"
        self.args = args
        self.n_jobs = args.n_jobs
        self.pool = joblib.Parallel(n_jobs=self.n_jobs)
        self.smi_file = args.smi_file
        self.mol_buffer = {}
        if self.smi_file is not None:
            self.all_smiles = self.load_smiles_from_file(self.smi_file)
        else:
            data = MolGen(name = 'ZINC')
            self.all_smiles = data.get_data()['smiles'].tolist()
            
        self.sa_scorer = tdc.Oracle(name = 'SA')
        self.diversity_evaluator = tdc.Evaluator(name = 'Diversity')
        self.filter = tdc.chem_utils.oracle.filter.MolFilter(filters = ['PAINS', 'SureChEMBL', 'Glaxo'], property_filters_flag = False)

    def load_smiles_from_file(self, file_name):
        with open(smi_file) as f:
            return self.pool(delayed(canonicalize)(s.strip()) for s in f)
            
    def sanitize(self, mol_list):
        new_mol_list = []
        smiles_set = set()
        for mol in mol_list:
            if mol is not None:
                try:
                    smiles = Chem.MolToSmiles(mol)
                    if smiles is not None and smiles not in smiles_set:
                        smiles_set.add(smiles)
                        new_mol_list.append(mol)
                except ValueError:
                    print('bad smiles')
        return new_mol_list
        
    def sort_buffer(self):
        self.mol_buffer = dict(sorted(self.mol_buffer.items(), key=lambda kv: kv[1][0], reverse=True))
            
    def score_mol(self, oracle_func, mol_list):
        score_list = []
        for mol in mol_list:
            smi = Chem.MolToSmiles(mol)
            try:
                _ = self.mol_buffer[smi]
                score_list.append(_[0])
            except:
                score = oracle_func(smi)
                self.mol_buffer[smi] = [score, len(self.mol_buffer)+1]
                score_list.append(score)
                
        self.sort_buffer()
        return score_list
    
    def log_intermediate(self, mols=None, scores=None):

        if mols is None and scores is None:
            # If not spefcified, log current top-100 mols in buffer
            temp_top100 = list(self.mol_buffer.items())[:100]
            smis = [item[0] for item in temp_top100]
            scores = [item[1][0] for item in temp_top100]
        else:
            # Otherwise, log the input moleucles
            smis = [Chem.MolToSmiles(m) for m in mols]
        
        # Uncomment this line if want to log top-10 moelucles figures, so as the best_mol key values.
        # temp_top10 = list(self.mol_buffer.items())[:10]

        wandb.log({
            "avg_top1": np.max(scores), 
            "avg_top10": np.mean(sorted(scores, reverse=True)[:10]), 
            "avg_top100": np.mean(scores), 
            "avg_sa": np.mean(self.sa_scorer(smis)),
            "diversity_top100": self.diversity_evaluator(smis),
            "n_oracle": len(self.mol_buffer),
            # "best_mol": wandb.Image(Draw.MolsToGridImage([Chem.MolFromSmiles(item[0]) for item in temp_top10], 
            #           molsPerRow=5, subImgSize=(200,200), legends=[f"f = {item[1][0]:.3f}, #oracle = {item[1][1]}" for item in temp_top10]))
        })
    
    def log_result(self):
        
        log_num_oracles = [100, 500, 1000, 3000, 5000, 10000]
        
        results = list(sorted(self.mol_buffer.items(), key=lambda kv: kv[1][1], reverse=False))
        if len(results) > 10000:
            results = results[:10000]
        
        results_all_level = []
        for n_o in log_num_oracles:
            results_all_level.append(sorted(results[:n_o], key=lambda kv: kv[1][0], reverse=True))
        
        # Currently logging the top-100 moelcules, will update to PDD selection later
        data = [[i+1, results_all_level[-1][i][1][0], results_all_level[-1][i][1][1], \
                wandb.Image(Draw.MolToImage(Chem.MolFromSmiles(results_all_level[-1][i][0]))), results_all_level[-1][i][0]] for i in range(100)]
        columns = ["Rank", "Score", "#Oracle", "Image", "SMILES"]
        wandb.log({"Top 100 Molecules": wandb.Table(data=data, columns=columns)})
        
        # Log batch metrics at various oracle calls
        data = [[log_num_oracles[i]] + self._analyze_results(r) for i, r in enumerate(results_all_level)]
        columns = ["#Oracle", "avg_top100", "avg_top10", "avg_top1", "Diversity", "avg_SA", "%Pass", "Top-1 Pass"]
        wandb.log({"Batch metrics at various level": wandb.Table(data=data, columns=columns)})
        
    def save_result(self, suffix=None):

        if suffix is None:
            output_file_path = os.path.join(self.args.output_dir, 'results.yaml')
        else:
            output_file_path = os.path.join(self.args.output_dir, 'results_' + suffix + '.yaml')

        self.sort_buffer()
        with open(output_file_path, 'w') as f:
            yaml.dump(self.mol_buffer, f, sort_keys=False)
    
    def _analyze_results(self, results):
        results = results[:100]
        scores_dict = {item[0]: item[1][0] for item in results}
        smis = [item[0] for item in results]
        scores = [item[1][0] for item in results]
        smis_pass = self.filter(smis)
        return [np.mean(scores), 
                 np.mean(scores[:10]), 
                 np.max(scores), 
                 self.diversity_evaluator(smis), 
                 np.mean(self.sa_scorer(smis)), 
                 float(len(smis_pass) / 100), 
                 np.max([scores_dict[s] for s in smis_pass])]
        
    def _optimize(self, oracle, config):
        raise NotImplementedError
            
    def hparam_tune(self, oracle, hparam_space, hparam_default, count=5, seed=0):
        hparam_space["name"] = hparam_space["name"] + "_" + oracle.name
        np.random.seed(seed)
        
        def _func():
            with wandb.init(config=hparam_default) as run:
                config = wandb.config
                self._optimize(oracle, config)
            self.mol_buffer = {}

        sweep_id = wandb.sweep(hparam_space)
        # wandb.agent(sweep_id, function=_func, count=count, project=self.model_name + "_" + oracle.name)
        wandb.agent(sweep_id, function=_func, count=count, entity="mol_opt")
        
    def optimize(self, oracle, config, seed=0):
        run = wandb.init(project=self.model_name + "_" + oracle.name, config=config, reinit=True, entity="mol_opt")
        np.random.seed(seed)
        self._optimize(oracle, config)
        self.log_result()
        self.save_result(self.model_name + "_" + oracle.name + "_" + str(seed))
        self.mol_buffer = {}
        run.finish()

    def production(self, oracle, config, num_runs=5):
        seeds = [2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53, 59, 61, 67, 71, 73, 79, 83, 89, 97]
        seeds = seeds[:num_runs]
        for seed in seeds:
            self.optimize(oracle, config, seed)
            self.mol_buffer = {}

