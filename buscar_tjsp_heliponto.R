#!/usr/bin/env Rscript
# ============================================================================
# Busca de jurisprudência sobre helipontos no TJSP (CJSG)
# Usa o endpoint público do ESAJ sem necessidade de autenticação.
# ============================================================================

suppressPackageStartupMessages({
  library(httr)
  library(rvest)
  library(xml2)
  library(jsonlite)
})

# ---------------------------------------------------------------------------
# 1. Configuração
# ---------------------------------------------------------------------------

ESAJ_BASE     <- "https://esaj.tjsp.jus.br"
CJSG_SEARCH   <- paste0(ESAJ_BASE, "/cjsg/resultadoCompleta.do")
CJSG_PAGE     <- paste0(ESAJ_BASE, "/cjsg/trocaDePagina.do")
CJSG_GATEWAY  <- paste0(ESAJ_BASE, "/cjsg/consultaCompleta.do?gateway=true")
CJSG_PDF      <- paste0(ESAJ_BASE, "/cjsg/getArquivo.do")
RESULTS_DIR   <- "tjsp_htmls"
OUTPUT_JSON   <- "tjsp_acordaos_heliponto.json"

TERMO_BUSCA   <- "heliponto"
TIPO_DECISAO  <- "A"   # A = Acórdãos, D = Monocráticas

# ---------------------------------------------------------------------------
# 2. Submeter busca (POST)
# ---------------------------------------------------------------------------

cat("=== Busca TJSP CJSG: '", TERMO_BUSCA, "' ===\n", sep = "")

# Warmup: obter cookies da sessão
cat("Acessando gateway...\n")
gateway <- GET(
  CJSG_GATEWAY,
  config(ssl_verifypeer = FALSE),
  add_headers(Accept = "text/html; charset=latin1;"),
  user_agent("Mozilla/5.0 (X11; Linux x86_64)")
)

# POST da busca
cat("Submetendo busca...\n")
resp <- POST(
  CJSG_SEARCH,
  config(ssl_verifypeer = FALSE),
  add_headers(Accept = "text/html; charset=latin1;"),
  user_agent("Mozilla/5.0 (X11; Linux x86_64)"),
  encode = "form",
  body = list(
    `conversationId`                      = "",
    `dados.buscaInteiroTeor`              = TERMO_BUSCA,
    `dados.pesquisarComSinonimos`         = "S",
    `dados.pesquisarComSinonimos`         = "S",
    `dados.buscaEmenta`                   = "",
    `dados.nuProcOrigem`                  = "",
    `dados.nuRegistro`                    = "",
    `agenteSelectedEntitiesList`          = "",
    `contadoragente`                      = "0",
    `contadorMaioragente`                 = "0",
    `codigoCr`                            = "",
    `codigoTr`                            = "",
    `nmAgente`                            = "",
    `juizProlatorSelectedEntitiesList`    = "",
    `contadorjuizProlator`                = "0",
    `contadorMaiorjuizProlator`           = "0",
    `codigoJuizCr`                        = "",
    `codigoJuizTr`                        = "",
    `nmJuiz`                              = "",
    `classesTreeSelection.values`         = "",
    `classesTreeSelection.text`           = "",
    `assuntosTreeSelection.values`        = "",
    `assuntosTreeSelection.text`          = "",
    `comarcaSelectedEntitiesList`         = "",
    `contadorcomarca`                     = "1",
    `contadorMaiorcomarca`                = "1",
    `cdComarca`                           = "",
    `nmComarca`                           = "",
    `secoesTreeSelection.values`          = "",
    `secoesTreeSelection.text`            = "",
    `dados.dtJulgamentoInicio`            = "",
    `dados.dtJulgamentoFim`               = "",
    `dados.dtRegistroInicio`              = "",
    `dados.dtRegistroFim`                 = "",
    `dados.origensSelecionadas`           = "T",
    `dados.origensSelecionadas`           = "R",
    `tipoDecisaoSelecionados`             = TIPO_DECISAO,
    `dados.ordenacao`                     = "dtPublicacao"
  )
)

cat("Status POST:", status_code(resp), "\n")

# Guardar cookies para paginação
cookies_resp <- cookies(resp)

# ---------------------------------------------------------------------------
# 3. Buscar página 1 e descobrir total
# ---------------------------------------------------------------------------

cat("Buscando página 1...\n")
pg1 <- GET(
  CJSG_PAGE,
  query = list(tipoDeDecisao = TIPO_DECISAO, pagina = 1),
  config(ssl_verifypeer = FALSE),
  add_headers(Accept = "text/html; charset=latin1;"),
  user_agent("Mozilla/5.0 (X11; Linux x86_64)"),
  set_cookies(.cookies = setNames(cookies_resp$value, cookies_resp$name))
)

html1 <- content(pg1, as = "text", encoding = "latin1")

# Descobrir total de resultados
total_text <- html1 |>
  read_html() |>
  html_nodes(xpath = "//td[contains(., 'Resultados')]") |>
  html_text()

total <- 0
if (length(total_text) > 0) {
  nums <- regmatches(total_text[1], gregexpr("\\d+", total_text[1]))[[1]]
  if (length(nums) >= 1) {
    total <- as.integer(tail(nums, 1))
  }
}

n_pages <- ceiling(total / 20)
cat(sprintf("Total de resultados: %d (%d páginas)\n", total, n_pages))

if (total == 0) {
  cat("Nenhum resultado encontrado. Verifique o termo de busca.\n")
  quit(status = 0)
}

# ---------------------------------------------------------------------------
# 4. Baixar todas as páginas
# ---------------------------------------------------------------------------

dir.create(RESULTS_DIR, showWarnings = FALSE)

# Salvar página 1
writeLines(html1, file.path(RESULTS_DIR, "pagina_001.html"))
cat("  Página 1 salva\n")

for (pg in seq_len(n_pages)[-1]) {
  Sys.sleep(1)  # rate limit
  cat(sprintf("  Página %d/%d...\n", pg, n_pages))

  resp_pg <- tryCatch(
    GET(
      CJSG_PAGE,
      query = list(tipoDeDecisao = TIPO_DECISAO, pagina = pg),
      config(ssl_verifypeer = FALSE),
      add_headers(Accept = "text/html; charset=latin1;"),
      user_agent("Mozilla/5.0 (X11; Linux x86_64)"),
      set_cookies(.cookies = setNames(cookies_resp$value, cookies_resp$name)),
      timeout(30)
    ),
    error = function(e) {
      cat("    ERRO:", conditionMessage(e), "\n")
      NULL
    }
  )

  if (!is.null(resp_pg) && status_code(resp_pg) == 200) {
    html_pg <- content(resp_pg, as = "text", encoding = "latin1")
    writeLines(html_pg, file.path(RESULTS_DIR, sprintf("pagina_%03d.html", pg)))
  }
}

# ---------------------------------------------------------------------------
# 5. Ler e extrair dados das páginas HTML
# ---------------------------------------------------------------------------

cat("\n=== Extraindo dados dos HTMLs ===\n")

htmls <- list.files(RESULTS_DIR, pattern = "\\.html$", full.names = TRUE)
resultados <- list()

for (f in htmls) {
  doc <- tryCatch(read_html(f, encoding = "UTF-8"), error = function(e) NULL)
  if (is.null(doc)) next

  # Classe/Assunto
  classe_assunto <- doc |> html_nodes(xpath = "//*[@class='assuntoClasse']") |> html_text(trim = TRUE)

  # Relator
  relator <- doc |> html_nodes(xpath = "//tr[2][@class='ementaClass2'][1]") |> html_text(trim = TRUE)

  # Comarca
  comarca <- doc |> html_nodes(xpath = "//*[@class='ementaClass2'][2]") |> html_text(trim = TRUE)

  # Órgão Julgador
  orgao <- doc |> html_nodes(xpath = "//*[@class='ementaClass2'][3]") |> html_text(trim = TRUE)

  # Data Julgamento
  data_julg <- doc |> html_nodes(xpath = "//*[@class='ementaClass2'][4]") |> html_text(trim = TRUE)

  # Data Publicação
  data_pub <- doc |> html_nodes(xpath = "//*[@class='ementaClass2'][5]") |> html_text(trim = TRUE)

  # Ementa
  ementa <- doc |> html_nodes(xpath = "//*[@class='mensagemSemFormatacao']") |> html_text(trim = TRUE)

  # Número do Processo
  processo <- doc |> html_nodes(xpath = "//*[@class='esajLinkLogin downloadEmenta']") |> html_text(trim = TRUE)

  # cdAcordao (para baixar PDF)
  cd_acordao <- doc |> html_nodes(xpath = "//a[@cdacordao]") |> html_attr("cdacordao")

  n <- max(length(ementa), length(processo), 1)

  for (i in seq_len(n)) {
    entry <- list(
      processo      = ifelse(i <= length(processo), gsub("[^0-9.-]", "", processo[i]), NA),
      classe_assunto = ifelse(i <= length(classe_assunto), classe_assunto[i], NA),
      relator       = ifelse(i <= length(relator), sub(".*?:\\s*", "", relator[i]), NA),
      comarca       = ifelse(i <= length(comarca), sub(".*?:\\s*", "", comarca[i]), NA),
      orgao_julgador = ifelse(i <= length(orgao), sub(".*?:\\s*", "", orgao[i]), NA),
      data_julgamento = ifelse(i <= length(data_julg), sub(".*?:\\s*", "", data_julg[i]), NA),
      data_publicacao = ifelse(i <= length(data_pub), sub(".*?:\\s*", "", data_pub[i]), NA),
      ementa        = ifelse(i <= length(ementa), ementa[i], NA),
      cd_acordao    = ifelse(i <= length(cd_acordao), cd_acordao[i], NA)
    )
    resultados[[length(resultados) + 1]] <- entry
  }
}

cat(sprintf("Total de acórdãos extraídos: %d\n", length(resultados)))

# ---------------------------------------------------------------------------
# 6. Salvar como JSON
# ---------------------------------------------------------------------------

json_out <- toJSON(resultados, auto_unbox = TRUE, pretty = TRUE, force = TRUE)
writeLines(json_out, OUTPUT_JSON)
cat(sprintf("Salvo em: %s\n", OUTPUT_JSON))

# ---------------------------------------------------------------------------
# 7. Resumo
# ---------------------------------------------------------------------------

cat("\n=== RESUMO ===\n")
for (i in seq_along(resultados)) {
  r <- resultados[[i]]
  cat(sprintf(
    "\n[%d] Processo: %s\n    Classe: %s\n    Relator: %s\n    Comarca: %s\n    Órgão: %s\n    Julgamento: %s\n    Ementa: %s\n",
    i,
    ifelse(is.na(r$processo), "?", r$processo),
    ifelse(is.na(r$classe_assunto), "?", substr(r$classe_assunto, 1, 60)),
    ifelse(is.na(r$relator), "?", substr(r$relator, 1, 40)),
    ifelse(is.na(r$comarca), "?", r$comarca),
    ifelse(is.na(r$orgao_julgador), "?", substr(r$orgao_julgador, 1, 40)),
    ifelse(is.na(r$data_julgamento), "?", r$data_julgamento),
    ifelse(is.na(r$ementa), "?", substr(r$ementa, 1, 150))
  ))
}

cat("\n=== FIM ===\n")
cat(sprintf("Arquivos HTML em: %s/\n", RESULTS_DIR))
cat(sprintf("JSON em: %s\n", OUTPUT_JSON))
