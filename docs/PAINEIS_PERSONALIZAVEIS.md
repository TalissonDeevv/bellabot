# Painéis totalmente personalizáveis

Os painéis públicos de **Tickets**, **Aprenda Inglês** e **Parceiros/Afiliados** usam o mesmo núcleo do gerador de embeds da Bella.

## O que pode ser alterado

- Embed V1: mensagem superior, até 10 embeds, título, descrição, cor, autor, campos, imagem, thumbnail, rodapé e data/hora.
- Components V2: até 5 containers, título, descrição, cor, thumbnail, textos, separadores, galerias e ordem dos itens.
- Elementos vazios permanecem removidos depois de salvar e reiniciar o bot.
- Os layouts V1 e V2 são salvos separadamente, permitindo manter uma versão própria para cada modo.

## Botões protegidos

Os botões funcionais de tickets, estudos e inscrições de parceiros não fazem parte do layout editável. Eles são anexados pela Bella ao publicar o painel, preservando `custom_id`, callbacks, permissões e funcionamento após reinicializações.

## Avisos de pausa

- Tickets: botão **Painel fechado** no painel administrativo.
- Aprenda Inglês: botão **Aviso pausado** no painel administrativo.
- Parceiros: botão **Painel pausado** no painel administrativo.

No aviso dos tickets podem ser usados os marcadores `{motivo}`, `{horarios}` e `{canal_duvidas}`. No Aprenda Inglês pode ser usado `{motivo}`.

## Compatibilidade

Configurações antigas continuam funcionando. O payload completo só passa a ser usado depois que o novo editor visual for salvo pela primeira vez.
