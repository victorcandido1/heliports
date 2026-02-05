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
```

## Arquivos locais aceitos

Se os servidores da ANAC ou GeoSampa estiverem indisponíveis, coloque os arquivos na raiz do projeto:

- **ANAC**: `anac_helipontos.csv`, `AerodromosPrivados.csv` ou `Helipontos.csv`
- **GeoSampa**: `helipontos.geojson`, `helipontos.json` ou `helipontos.shp`

## Saídas

| Arquivo | Descrição |
|---|---|
| `comparativo_helipontos_sp.csv` | Tabela comparativa com status consolidado |
| `mapa_helipontos_sp.html` | Mapa interativo (Folium) |

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
