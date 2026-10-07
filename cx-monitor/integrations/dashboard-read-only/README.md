# Conexão de leitura do onboarding — proposta pronta para revisão

O código `index.ts` está preparado, mas **não foi aplicado ao Atendimento Premium**.
A autorização atual permite consultar esse sistema somente para leitura. Criar ou
publicar uma função no backend precisa de autorização específica.

## Aplicação proposta

1. Conectar o Supabase do Atendimento Premium e confirmar o projeto correto.
2. Após autorização, publicar somente a função `cx-monitor-onboarding-read`.
   Configurar `verify_jwt = false` **apenas para essa função**: a função valida
   seu próprio token de máquina. Nunca desativar JWT das funções existentes.
3. Criar um segredo aleatório de pelo menos 32 caracteres em `CX_MONITOR_READ_TOKEN`
   no backend; colocar o mesmo segredo em `DASHBOARD_READ_TOKEN`, somente no CX Monitor.
   Usar o gerenciador de segredos; não colocar valores no chat, código ou logs.
4. Configurar `DASHBOARD_READ_URL` com a URL HTTPS dessa função no Supabase.
   `PONTADELANCA_SUPABASE_URL` e `PONTADELANCA_SERVICE_ROLE_KEY` já utilizados
   pelo proxy permanecem exclusivamente no backend da fonte.
5. Validar leitura completa real e um segundo ciclo; conferir o SLA com a tela.

A função só admite GET e duas consultas SELECT predefinidas. Não recebe SQL,
não altera registros e não aciona pull-onboarding-members. O resultado não
inclui CPF, aniversário, família, email, telefone ou inferência de Deal pelo nome.
Uma falha em qualquer página devolve 502; o monitor não interpreta dados parciais
como cobertura completa. A paginação não é uma transação única entre tabelas.

O cliente já está conectado ao ciclo de captura de cinco minutos. Sem URL/token,
registra `dashboard_read_configuration_missing` e não realiza requisição.
Snapshots antigos não são apresentados como atuais. Datas inválidas não confirmam
cumprimento de SLA. A integração classifica datas registradas; o histórico original
de replanejamentos ainda deve ser obtido da fonte para confirmar a previsão original.

## Dependência restante para análises completas

O serviço atual prepara dossiês de evidências; não tem credencial de um modelo de
análise. Para usar um agente nativo no Railway, ainda precisam ser definidos um
modelo disponível e sua credencial de API no gerenciador de segredos. Não colocar
uma chave de API antes de confirmar a opção de execução e a configuração do agente.

O agente deverá distinguir fatos, divergências, hipóteses, lacunas e ações; citar
IDs/links e datas; incluir as threads; não vincular homônimos a Deals; preservar
atrasos concluídos; diferenciar forms de handoff (0) e forms do Club do membro (7).
Ele não pode declarar revisão humana que não ocorreu. Antes de liberar publicação,
validar uma análise real e sua entrega integral somente em cx-monitoramento-piloto.
`SLACK_PUBLISH_ENABLED` permanece false até essa validação.
