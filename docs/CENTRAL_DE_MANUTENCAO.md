# Central de Manutenção da Bella

## Objetivo

A Central de Manutenção mantém a Bella conectada enquanto pausa os recursos públicos. O painel administrativo é persistente e publicado no canal configurado por `MAINTENANCE_PANEL_CHANNEL_ID` (padrão: `1561342090238234654`).

## Fluxo

1. Um administrador autorizado escolhe Manutenção, Melhorias, Atualização, Emergência ou Personalizar.
2. A configuração anterior da presença é salva integralmente.
3. A Bella aplica o status e a atividade de manutenção.
4. Tickets, inscrições de parceiros, inglês, achadinhos, sorteios e conversas públicas são bloqueados.
5. Cada interação recebe um aviso efêmero; menções recebem uma resposta pública controlada.
6. O heartbeat do Observatório passa a publicar `maintenance`.
7. Ao finalizar, a presença anterior é restaurada e o heartbeat volta a `online`.

## Persistência

- `global_maintenance`: estado, motivo, mensagem, responsável, previsão e snapshot da presença.
- `global_maintenance_panel`: servidor, canal e mensagem do painel fixo.
- O painel é registrado como view persistente e editado ou recriado na inicialização.

## Logs

O módulo `maintenance` integra a Central de Logs existente. Na inicialização, a Bella reutiliza a categoria configurada e cria ou reutiliza `🛠️╺╸manutencao`. São registrados início, conclusão e tentativas públicas bloqueadas, com redução de spam por usuário e sistema.

## Site

O heartbeat autenticado existente informa o estado `maintenance` sem novo segredo ou tabela. O site exibe o estado no rodapé e em `/status`. A ausência de heartbeat é mostrada como estado não confirmado, nunca como operacional.

## Segurança

- Todos os controles administrativos validam o RBAC no backend.
- O painel não desativa moderação, AutoMod, auditoria ou logs.
- A integração pública utiliza o segredo já configurado do Observatório.
- Nenhuma credencial é exibida ou armazenada na configuração de manutenção.
