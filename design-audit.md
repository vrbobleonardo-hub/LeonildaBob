# Design audit — adaptação dos blocos side-by-side

## Escopo

Revisão dos blocos que estavam com aparência de grid/template: cards lado a lado com pesos muito parecidos, especialmente em home, atuação e instituto.

## Diagnóstico

- Os grids de 2 e 3 colunas criavam ritmo repetitivo e pouco editorial.
- Cards com títulos grandes lado a lado competiam entre si e diminuíam a sensação premium.
- As seções “como funciona”, “pilares”, “programa” e “trajetória/instituto” precisavam de hierarquia, não de caixas iguais.
- FAQ dividido lateralmente pesava demais a página e dificultava leitura.

## Ajustes aplicados

- `concise-grid`: saiu de card duplo e virou bloco editorial em linhas.
- `process-steps`: saiu de três cards lado a lado e virou timeline vertical numerada.
- `practice-list`: saiu de grid 3x2 e virou lista editorial com título e descrição por linha.
- `institute-pillars`: saiu de três cards e virou sequência editorial.
- `program-grid`: saiu de cards lado a lado e virou lista de tópicos.
- `faq-section`: saiu de layout dividido e virou FAQ empilhado com cabeçalho forte.
- `document-section` e `contact-guidance`: passaram a priorizar leitura vertical.

## Limite da verificação

O navegador interno já havia bloqueado captura de `127.0.0.1` nesta sessão, então esta revisão foi feita pela estrutura de layout/CSS e validada por renderização HTTP + smoke test. A próxima revisão visual ideal é abrir `http://127.0.0.1:8000` manualmente e apontar os blocos que ainda parecerem pesados.

