import torch
import torch.nn as nn
import numpy as np
import times
import torch.optim as optim
import pickle
import copy,math
import tqdm
import dgl
from transformers import GPT2LMHeadModel, GPT2Tokenizer, GPT2Model, GPT2Config
import dgl.function as fn
from pytorch_pretrained_bert import OpenAIAdam

device = torch.device("cuda:0" if torch.cuda.is_available() else "cpu")

def handler_edges(edges_embed):
    embed = []
    for edge_embed in edges_embed:
        tmp = torch.sum(edge_embed,dim=2) / edge_embed.shape[1]
        embed.append(tmp.unsqueeze(0))
    return torch.cat(embed,dim=0)

def handler_graph(nodes_embed,edges_embed,edge_types):
    embed = []
    for i in range(len(nodes_embed)):
        # find speicfic edges for this graph
        edge_type = edge_types[i]
        edge_embed_cur = edges_embed[i]
        edge_type_embed = []
        for idx in edge_type:
          edge_type_embed.append(edge_embed_cur[idx])
        edge_embed = torch.cat(edge_type_embed)
        assert(edge_embed.shape[0]==edge_type.shape[0])
        tmp = torch.cat((nodes_embed[i],edge_embed),dim=0)
        tmp = torch.sum(tmp,dim=0) / tmp.shape[0]
        embed.append(tmp.unsqueeze(0))
    return torch.cat(embed,dim=0)

# try with no regularization
class RGCNLayer(nn.Module):
    def __init__(self, num_rels, in_feat=256, out_feat=256, num_bases=-1, bias=None,
                 activation=None, is_input_layer=False, is_output_layer=False):
        super(RGCNLayer, self).__init__()
        self.in_feat = in_feat
        self.out_feat = out_feat
        self.num_rels = num_rels
        self.num_bases = num_bases
        self.bias = bias
        self.activation = activation
        self.is_input_layer = is_input_layer
        self.is_output_layer = is_output_layer
        # self.start_trans = nn.Linear(768,256)
   

    def forward(self, g, embed_model):
        # if self.num_bases < self.num_rels:
        #     # generate all weights from bases (equation (3))
        #     weight = self.weight.view(self.in_feat, self.num_bases, self.out_feat)
        #     weight = torch.matmul(self.w_comp, weight).view(self.num_rels, self.in_feat, self.out_feat)

        if self.is_input_layer:
            def message_func(edges):             
                # node_hidden = self.start_trans(edges.src['h']).unsqueeze(1)
                names = edges.src['names']
                edge_types = edges.data['rel_type']
                msg = []
                for idx in range(edge_types.shape[0]):
                    name = id2entity[names[idx].tolist()]
                    edge_type = id2rel[edge_types[idx].tolist()[0]]
                    sr = name+' '+edge_type
                    input_ids = torch.LongTensor(tokenizer_gpt2.encode(sr)).unsqueeze(0).to(device)
                    sr_embed = embed_model.transformer.wte(input_ids).squeeze(0)
                    sr_embed = torch.sum(sr_embed,dim=0) / sr_embed.shape[0]
                    # print('sr_embed:',sr_embed.shape)
                    msg.append(sr_embed.unsqueeze(0))
                msg = torch.cat(msg)
                return {'msg': msg}

        # some functions are for experiment, not used in final implementation
        elif self.is_output_layer:
            def message_func(edges):
                # print('here in output')
                node_hidden = edges.src['h'].unsqueeze(1)
                core_weight = torch.bmm(node_hidden,self.weight[edges.data['rel_type'].squeeze(1)])
                msg = core_weight * edges.data['norm'].unsqueeze(-1)
                del core_weight
                msg = msg.squeeze(1)
                msg = self.end_trans(msg)
                # print('msg.shape:',msg.shape)
                return {'msg': msg}

        else:
            def message_func(edges):
                # print('here in hidden')
                node_hidden = edges.src['h'].unsqueeze(1)
                # print('node_hidden.shape:',node_hidden.shape)
                core_weight = torch.bmm(node_hidden,self.weight[edges.data['rel_type'].squeeze(1)])
                msg = core_weight * edges.data['norm'].unsqueeze(-1)
                del core_weight
                msg = msg.squeeze(1)
                # print('msg.shape:',msg.shape)
                return {'msg': msg}

        def apply_func(nodes):
            # print('here in apply')
            h = nodes.data['h']
            # print('h:',h)
            if self.bias:
                h = h + self.bias
            if self.activation:
                h = self.activation(h,inplace=True)
            return {'h': h}

        def revc_func(nodes):
            # print('here in revc func')
            # print(torch.sum(nodes.mailbox['msg'], dim=1).shape)
            return {'h': torch.sum(nodes.mailbox['msg'], dim=1)}

        g.update_all(message_func, revc_func, apply_func)

class RGCNModel(nn.Module):
    def __init__(self, num_rels, num_bases=-1, num_hidden_layers=0):
        super(RGCNModel, self).__init__()
        # self.h_dim = h_dim
        # self.out_dim = out_dim
        self.num_rels = num_rels
        self.num_bases = num_bases
        self.num_hidden_layers = num_hidden_layers

        # create rgcn layers
        self.build_model()
        

    def build_model(self):
        self.layers = nn.ModuleList()
        # input to hidden
        i2h = self.build_input_layer()
        self.layers.append(i2h)
        # # hidden to hidden
        # for _ in range(self.num_hidden_layers):
        #     h2h = self.build_hidden_layer()
        #     self.layers.append(h2h)
        # # hidden to output
        # h2o = self.build_output_layer()
        # self.layers.append(h2o)

    # initialize feature for each node
    def build_input_layer(self):
        return RGCNLayer(self.num_rels, activation=F.relu, is_input_layer=True)
        # return RGCNLayer(self.num_rels, is_input_layer=True)

    def build_hidden_layer(self):
        return RGCNLayer(self.num_rels, activation=F.relu)
        # return RGCNLayer(self.num_rels)

    def build_output_layer(self):
        return RGCNLayer(self.num_rels, activation=F.relu, is_output_layer=True)
        # return RGCNLayer(self.num_rels, is_output_layer=True)

    def forward(self,gs,names,edge_types,embed_model):
        node_results = []
        # edge_results = []
        for i in range(len(gs)):
            g = gs[i]
            g.ndata['names'] = torch.LongTensor(names[i]).to(device) # [node_num, hidden]
            g.edata['rel_type'] = edge_types[i].to(device) # [edge_num, 1]
            # g.edata['norm'] = edge_norms[i].to(device) # [edge_num, 1]

            for layer in self.layers:
                layer(g,embed_model)
            nodes_embed = g.ndata.pop('h')
            # edge_results.append(self.layers[-1].weight)
            g.edata.pop('rel_type')
            g.ndata.pop('names')
            nodes_embed = torch.sum(nodes_embed,dim=0) / nodes_embed.shape[0]
            node_results.append(nodes_embed.unsqueeze(0))
          

        return node_results


class R_GCN_GPT2(nn.Module):
    def __init__(self, num_rels=30, num_bases=-1, num_hidden_layers=0):
        super(R_GCN_GPT2, self).__init__()
        self.rgcn_model = RGCNModel(num_rels)
        # self.path_embedding = nn.Embedding(50257, 768)
        self.gpt2_model = GPT2LMHeadModel.from_pretrained('gpt2-large')
        # self.weight_trans = nn.Linear(256,768)
        # nn.init.xavier_uniform_(self.weight_trans.weight)

    def get_node_embedding(self,entity_ids):
        features = []
        for entity_id_list in entity_ids:
          entity_embeds = []
          for entity_id in entity_id_list:
            entity = id2entity[entity_id]
            input_ids = torch.LongTensor(tokenizer_bert.encode(entity)).unsqueeze(0).to(device)
            entity_embed = self.node_embedding(input_ids=input_ids)[0].squeeze(0)
            entity_embed = torch.sum(entity_embed,dim=0) / entity_embed.shape[0]
            entity_embeds.append(entity_embed.unsqueeze(0))
            feature = torch.cat(entity_embeds)
          features.append(feature)
        return features


    def generate_ckg(self, batch):
        with torch.no_grad():
            prev_pred = batch['sr_ids']
            mask = batch['sr_mask']
            sentence = []
            past = None
            length = 1
            # decoding loop
            for i in range(20):       
                # print('mask.shape:',mask.shape)
                # print('input_embed.shape:',input_embed.shape)
                logits, past = self.gpt2_model(input_ids=prev_pred,attention_mask=mask,past=past)
                mask = F.pad(mask, (0, 1), "constant", 1.0) # add 1 to the last of the sentence mask
                # print('mask:',mask.shape)
                # logits = model.gpt2_model(attention_mask=mask,inputs_embeds=input_embed)
                # logits = logits[0]
                
                # logits, past = decoder(input_ids=prev_pred, past=past, attention_mask=mask)
                logits = logits[:,-1,:]
                # print('logits:',logits.shape)
                logits = logits.squeeze(1) / temperature
                logits = top_k_logits(logits, k=top_k)
                probs = F.softmax(logits, dim=-1)
                prev_pred = torch.multinomial(probs, num_samples=1)
                sentence.append(prev_pred)
                # if prev_pred[0][0] == 50256:
                #     break
                length += 1
                # print('prev_pred:',prev_pred)
                # input_embed = self.gpt2_model.transformer.wte(prev_pred).squeeze(0)

            sentence = torch.cat(sentence, dim=-1)
            # print('sentence:',sentence.shape)

        return sentence

    def forward_ckg(self,batch):
        sen_ids = batch['sen_ids']
        # sr_ids = batch['sr_ids']
        attention_mask = batch['sen_mask']
        # mask = torch.LongTensor(batch['mask']).to(device)
        # sr_embedding, sr_mask = self.get_sr_embedding(heads,rels,pad_length)
        # tail_embedding = self.get_tail_embedding(tails)
        # input_embed = torch.cat((sr_embedding,tail_embedding),dim=1)
        # mask = torch.cat((sr_mask,mask),dim=1)
        # print(mask[0])
        
        logits = self.gpt2_model(input_ids=sen_ids,attention_mask=attention_mask)
        
        return logits


    def forward(self, batch):
        g = batch['g']
        path = batch['path']
        path_mask = batch['path_mask']
        edge_types = batch['edge_types']
        edge_norms = batch['edge_norms']
        # features = batch['features']
        names = batch['names']

        # features = self.get_node_embedding(names)

        nodes_embed = self.rgcn_model(g,names,edge_types,self.gpt2_model) #[bsz,node_num,hidden], [rel_num,256]
        graph_embed = torch.cat(nodes_embed).unsqueeze(1)
        # print('nodes_embed:',nodes_embed.shape)

        path_embed = self.gpt2_model.transformer.wte(path)

        # print('path_embed:',path_embed.shape)


        # print('graph_embed:',graph_embed.shape)
        # print('path_mask:',path_mask.shape)
        input_embed = torch.cat((graph_embed,path_embed),dim=1)
        graph_embed = graph_embed.cpu()
        torch.cuda.empty_cache()

        # print('input_embed.shape:',input_embed.shape)
        ones = torch.ones(path_mask.shape[0],1).to(device)
        # print('ones.shape:',ones.shape)
        mask = torch.cat([ones,path_mask],dim=1)
        del ones
        # print('mask:',mask.shape)
        logits = self.gpt2_model(attention_mask=mask,inputs_embeds=input_embed)

        return logits

