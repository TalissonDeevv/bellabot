# Alternativa: Google Cloud e2-micro (grátis, 1 GB)

O tutorial principal de hospedagem está no [README](../README.md#hospedagem-contabo-ou-qualquer-vps-ubuntu).
Este arquivo cobre só o que muda no Google Cloud.

Medido: a Bella sem Chromium usa ~180 MB (pico 276 MB). Com Ubuntu e FreeLLMAPI
o total fica em ~550–800 MB. O instalador cria 2 GB de swap sozinho quando o
servidor tem menos de 2 GB de RAM.

## Criar a VM sem gerar cobrança

1. Conta em https://cloud.google.com/free.
2. Faturamento > Orçamentos e alertas > orçamento de **R$ 1** com alertas em 50%,
   90% e 100%.
3. Compute Engine > Instâncias de VM > Criar instância:

| Campo | Valor |
|---|---|
| Região | **us-central1**, us-west1 ou us-east1 |
| Tipo de máquina | **e2-micro** |
| Disco | **Ubuntu 24.04 LTS (x86/64)**, **Disco permanente padrão**, **30 GB** |
| Firewall | não marque HTTP nem HTTPS |

Pode gerar cobrança: outra região, outro tipo de máquina, uma segunda VM, disco
Balanced/SSD ou maior que 30 GB, mais de 1 GB/mês de tráfego de saída. Usar
mais ou menos RAM não gera cobrança.

## Diferenças em relação ao README

- O SSH é pelo botão **SSH** da lista de VMs (abre no navegador). O usuário já
  vem criado com sudo, então pule o passo de criar usuário.
- O túnel para o painel da FreeLLMAPI usa o Google Cloud CLI:

```bash
gcloud compute ssh NOME_DA_VM --zone ZONA -- -L 3001:127.0.0.1:3001
```

(troque 3001 se o instalador escolheu outra porta; ele mostra no final).
