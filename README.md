# PJ Distribuidor de Processos

Bot Discord em Python com exatamente quatro comandos:
- `/sortear`
- `/sortear2 numero_processo`
- `/adicionar ministro`
- `/remover ministro`

## O que faz
- Observa somente o canal Fórum configurado.
- Cadastra tópicos que tenham a tag exata `Sem Relator`; outras tags são ignoradas.
- Se encontrar `Processo Sentenciado` ou `Processo Arquivado`, bloqueia o processo para novos sorteios.
- Não lê nem copia mensagens, imagens, anexos ou documentos dos tópicos.
- Guarda ministros, processos e histórico em SQLite.
- Cada processo aceita até 10 redistribuições. O sorteio inicial não conta.
- Na redistribuição, exclui o ministro atual; ministros anteriores podem voltar a ser sorteados.
- Os quatro comandos exigem permissão de Administrador.

## Antes de iniciar
1. No Discord Developer Portal, ative **Server Members Intent** em Bot > Privileged Gateway Intents.
2. Convide o bot ao servidor de teste com as permissões necessárias para ver o fórum, ver tópicos e enviar mensagens. Para simplificar o primeiro teste, o convite pode usar permissões de Administrador.
3. Copie `.env.example` para `.env`.
4. Preencha `DISCORD_TOKEN`, `GUILD_ID` e `FORUM_CHANNEL_ID`.
5. Use um token recém-gerado. Nunca publique o token em chat, prints ou repositórios.
6. Instale dependências: `pip install -r requirements.txt`
7. Inicie: `python bot.py`

## Como obter os IDs
No Discord, habilite Modo Desenvolvedor em Configurações > Avançado. Depois toque/clique com o botão direito no servidor e no canal Fórum e escolha **Copiar ID**.

## Formato do número do processo
Por padrão, o número precisa aparecer no título do tópico como `002/2026` ou `Processo 002/2026`.
Se o título usar outro padrão, ajuste `PROCESS_REGEX` no `.env`.

## Persistência e hospedagem
O arquivo `pj_distribuidor.sqlite3` guarda os dados. Em hospedagem, use disco/volume persistente e mantenha esse arquivo entre reinicializações. Se o serviço apagar arquivos locais ao reiniciar, os cadastros e históricos serão perdidos.

## Observações importantes
- O bot não consegue ser mantido online apenas pelo link de convite; precisa estar executando em um computador ou serviço de hospedagem.
- A implementação monitora criação e atualização de tópicos do fórum e faz uma leitura inicial dos tópicos ativos/arquivados acessíveis ao bot.
- O processo é identificado pelo número no título. Não renomeie tópicos para outro número depois de cadastrados.
- Um processo encerrado permanece bloqueado no banco, mesmo que a tag `Sem Relator` seja aplicada depois.
