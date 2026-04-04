import pandas as pd
import json
import re
from pathlib import Path
import os
os.chdir('/home/user/heliports')

# Load all data sources
anac = pd.read_csv('anac_helipontos.csv', encoding='utf-8-sig', sep=';')
smul = pd.read_excel('smul_helipontos.xlsx')
with open('helipontos.geojson') as f:
    gs_data = json.load(f)
with open('aisweb_helipontos_cache.json') as f:
    aisweb = json.load(f)
with open('tjsp_acordaos_classificados.json') as f:
    tjsp = json.load(f)
with open('doc_processos_smul.json') as f:
    doc_procs = json.load(f)

# ANAC stats
print("=== ANAC ===")
print(f"Total SP: {len(anac)}")
print(f"VFR: {(anac['Operação']=='VFR').sum()}")
print(f"VFR DIURNA: {(anac['Operação']=='VFR DIURNA').sum()}")
print(f"PRIV: {(anac['Tipo']=='PRIV').sum()}")
print(f"MIL: {(anac['Tipo']=='MIL').sum()}")

# Municipios
print(f"\nMunicípios:")
for m, c in anac['Município'].value_counts().head(10).items():
    print(f"  {m}: {c}")

# SMUL stats
print("\n=== SMUL ===")
print(f"Total: {len(smul)}")
print(f"Columns: {list(smul.columns)}")
smul['val'] = pd.to_datetime(smul['VALIDADE AUTO'], errors='coerce')
vigentes = (smul['val'] >= pd.Timestamp.now()).sum()
print(f"Vigentes: {vigentes}")
print(f"Vencidas: {len(smul) - vigentes}")

# GeoSampa stats
deferidos = sum(1 for f in gs_data['features'] if f['properties'].get('st_deferimento_processo_administrativo') == 'Deferido')
indeferidos = sum(1 for f in gs_data['features'] if f['properties'].get('st_deferimento_processo_administrativo') == 'Indeferido')
print(f"\n=== GeoSampa ===")
print(f"Total: {len(gs_data['features'])}")
print(f"Deferidos: {deferidos}")
print(f"Indeferidos: {indeferidos}")

# Distritos
from collections import Counter
distritos = Counter()
for feat in gs_data['features']:
    d = feat['properties'].get('nm_distrito_municipal', '')
    if d:
        distritos[d] += 1
print("Distritos:")
for d, c in distritos.most_common():
    print(f"  {d}: {c}")

# AISWEB - heliports with dimensions >= 21x21
print(f"\n=== AISWEB ===")
print(f"Cache entries: {len(aisweb)}")
big_heliports = []
for oaci, data in aisweb.items():
    if data is None:
        continue
    dim = data.get('dimensoes', '')
    if dim:
        m = re.search(r'(\d+)\s*[×x]\s*(\d+)', dim)
        if m:
            w, h = int(m.group(1)), int(m.group(2))
            if w >= 21 or h >= 21:
                big_heliports.append({
                    'oaci': oaci,
                    'dimensoes': dim,
                    'mtow': data.get('mtow_display', ''),
                    'superficie': data.get('superficie', ''),
                    'w': w, 'h': h,
                })

with_dims = sum(1 for v in aisweb.values() if v and v.get('dimensoes'))
print(f"With dimensions: {with_dims}")
print(f">=21x21: {len(big_heliports)}")

# Cross reference big heliports with ANAC, SMUL, GeoSampa
print(f"\n=== HELIPONTOS >= 21x21 ===")

# Build lookup maps
anac_by_oaci = {}
for _, r in anac.iterrows():
    o = str(r['Código OACI']).strip()
    anac_by_oaci[o] = {
        'nome': r.get('Nome', ''),
        'ciad': r.get('CIAD', ''),
        'operacao': r.get('Operação', ''),
        'tipo': r.get('Tipo', ''),
        'municipio': r.get('Município', ''),
    }

gs_by_oaci = {}
for feat in gs_data['features']:
    p = feat['properties']
    o = (p.get('tx_endereco_heliponto') or '').strip()
    if o:
        gs_by_oaci[o] = {
            'nome_gs': p.get('nm_empreendimento_heliponto', ''),
            'situacao': p.get('st_deferimento_processo_administrativo', ''),
            'distrito': p.get('nm_distrito_municipal', ''),
            'ciclos': p.get('qt_total_ciclo_permitido', 0),
            'endereco': (p.get('tp_logradouro_heliponto') or '') + ' ' + (p.get('nm_logradouro_heliponto') or ''),
        }

smul_by_name = {}
for _, r in smul.iterrows():
    nome = str(r.get('NOME DO HELIPONTO', '')).upper().strip()
    smul_by_name[nome] = {
        'auto': r.get('Nº AUTO LICENÇA', ''),
        'validade': str(r.get('VALIDADE AUTO', ''))[:10],
        'vigente': r['val'] >= pd.Timestamp.now() if pd.notna(r['val']) else False,
        'processo': r.get('Processo', ''),
        'proprietario': r.get('PROPRIETÁRIO', ''),
    }

# DOC data
with open('doc_nomes_results.json') as f:
    doc_names = json.load(f)

# Print header
print("| OACI | Nome | Dimensões | MTOW | Operação | GeoSampa | SMUL | Distrito | Ciclos/dia |")
print("|------|------|-----------|------|----------|----------|------|----------|------------|")

# Print full table
for h in sorted(big_heliports, key=lambda x: -(x['w']*x['h'])):
    oaci = h['oaci']
    anac_info = anac_by_oaci.get(oaci, {})
    gs_info = gs_by_oaci.get(oaci, {})
    nome = anac_info.get('nome', '') or gs_info.get('nome_gs', '')

    # Find SMUL match
    smul_info = None
    nome_upper = nome.upper().strip()
    for sn, si in smul_by_name.items():
        if len(sn) >= 4 and (sn in nome_upper or nome_upper in sn):
            smul_info = si
            break

    gs_sit = gs_info.get('situacao', 'Sem cadastro')
    if not gs_sit:
        gs_sit = 'Sem cadastro'
    smul_status = 'Sem licença'
    if smul_info:
        smul_status = f"Vigente até {smul_info['validade']}" if smul_info['vigente'] else f"Vencida ({smul_info['validade']})"

    print(f"| {oaci} | {nome[:40]} | {h['dimensoes']} | {h['mtow']} | {anac_info.get('operacao','')} | {gs_sit} | {smul_status} | {gs_info.get('distrito','')} | {gs_info.get('ciclos','')} |")

# TJSP stats
print(f"\n=== TJSP ===")
print(f"Acórdãos total: {len(tjsp)}")
temas = Counter()
resultados = Counter()
comarcas = Counter()
anos = Counter()
for j in tjsp:
    for t in j.get('temas', []):
        temas[t] += 1
    r = j.get('resultado', 'indefinido')
    resultados[r] += 1
    c = j.get('comarca', '')
    comarcas[c] += 1
    d = j.get('data', '')
    if d:
        try:
            ano = d.split('/')[-1]
            anos[ano] += 1
        except:
            pass

print("Temas:")
for t, c in temas.most_common():
    print(f"  {t}: {c}")
print("Resultados:")
for r, c in resultados.most_common():
    print(f"  {r}: {c}")
print("Anos:")
for a, c in sorted(anos.items()):
    print(f"  {a}: {c}")

# DOC stats
print(f"\n=== DOC ===")
n_6068 = sum(1 for p in doc_procs if p.startswith('6068'))
n_6027 = sum(1 for p in doc_procs if p.startswith('6027'))
n_6050 = sum(1 for p in doc_procs if p.startswith('6050'))
print(f"Processos SMUL (6068.*) no DOC: {n_6068}")
print(f"Processos CADES (6027.*): {n_6027}")
print(f"Processos fiscalização (6050.*): {n_6050}")
print(f"Total processos: {len(doc_procs)}")

# DOC fines
with open('doc_multas_helipontos.json') as f:
    multas = json.load(f)
print(f"\nMultas DOC: {len(multas)}")
for m in multas:
    print(f"  {m['data']}: {m.get('auto_multa','')} / {m.get('auto_fisc','')}")

# DOC names summary
print(f"\nHelipontos com processos DOC: {len(doc_names)}")
total_doc_procs = sum(v.get('total_doc', 0) for v in doc_names.values())
print(f"Total documentos DOC encontrados: {total_doc_procs}")

# SMUL detail
print("\n=== SMUL DETAIL ===")
for _, r in smul.iterrows():
    nome = r.get('NOME DO HELIPONTO', '')
    val = str(r.get('VALIDADE AUTO', ''))[:10]
    auto = r.get('Nº AUTO LICENÇA', '')
    print(f"  {nome} | Auto: {auto} | Val: {val}")
