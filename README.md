# Helipontos SP — Cruzamento GeoSampa × ANAC

Script para baixar, cruzar e mapear os helipontos da cidade de São Paulo, comparando a base da Prefeitura (GeoSampa) com a base nacional (ANAC).

## Instalação

```bash
pip install -r requirements.txt
```

## Uso

```bash
# Modo automático (baixa dados da ANAC e GeoSampa via API)
python helipontos_sp.py

# Com arquivos locais (caso os servidores estejam indisponíveis)
python helipontos_sp.py --anac-csv anac_helipontos.csv --geosampa-file helipontos.geojson

# Buffer customizado (padrão: 100m)
python helipontos_sp.py --buffer 150

# Usar apenas cache do AISWEB (sem buscar dimensões/MTOW online)
python helipontos_sp.py --no-fetch-aisweb
```

## Arquivos locais aceitos

Se os servidores da ANAC ou GeoSampa estiverem indisponíveis, coloque os arquivos na raiz do projeto:

- **ANAC**: `anac_helipontos.csv`, `AerodromosPrivados.csv` ou `Helipontos.csv`
- **GeoSampa**: `helipontos.geojson`, `helipontos.json` ou `helipontos.shp`

## Saídas

| Arquivo | Descrição |
|---|---|
| `comparativo_helipontos_sp.csv` | Tabela comparativa com status consolidado |
| `mapa_helipontos_sp.html` | Mapa interativo (Folium) — inclui botão **Dashboard** que abre o relatório em overlay |
| `relatorio_helipontos_sp.html` | Dashboard/relatório (abre em overlay ao clicar no botão) |
| `aisweb_helipontos_cache.json` | Cache de dimensões e MTOW do AISWEB (gerado automaticamente) |

## Status consolidado

| Status | Cor no mapa | Significado |
|---|---|---|
| `REGULAR` | Verde | Presente em ambas as bases, ativo na ANAC |
| `DIVERGENTE_ANAC_INATIVO` | Amarelo | No GeoSampa, mas inativo/vencido na ANAC |
| `NÃO_CADASTRADO_ANAC` | Vermelho | No GeoSampa, mas ausente na ANAC |
| `NÃO_CADASTRADO_PREFEITURA` | Roxo | Na ANAC, mas ausente no GeoSampa |

## Fontes de dados

- **ANAC**: [Portal de Dados Abertos](https://www.gov.br/anac/pt-br/assuntos/regulados/aeroportos-e-aerodromos/lista-de-aerodromos-civis-cadastrados)
- **GeoSampa**: [WFS Prefeitura de São Paulo](http://wfs.geosampa.prefeitura.sp.gov.br/geoserver/geoportal/ows)
- **AISWEB**: [Aeródromos/ROTAER](https://aisweb.decea.mil.br/?i=aerodromos) — dimensões e peso máximo (MTOW) por heliponto

## Deploy (Railway)

Para publicar no Railway, gere os arquivos (`python helipontos_sp.py`) e sirva os HTMLs como estáticos:

- `mapa_helipontos_sp.html` — página principal (abre o mapa)
- `relatorio_helipontos_sp.html` — carregado em overlay ao clicar em "Dashboard"

Ambos devem estar no mesmo diretório/base URL para o iframe do dashboard funcionar.
