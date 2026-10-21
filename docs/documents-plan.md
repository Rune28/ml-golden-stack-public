# Document graph: documents, authors and concepts, linked

The stage after the LLM conveyor ([llm-plan.md](llm-plan.md)). Today retrieval is a point search: the k chunks closest to a question. The next questions are about many documents at once: "show me papers on squats", "what has this author written about knees", "books on sport". They need documents, authors and topics as entities with keys, and links between them, so a question about a topic finds every document on it, under whatever name or language the topic appears there.

## Why this layout

The model is Data Vault: **hubs** are business entities with a business key, **links** are relations between hubs, **satellites** are attributes of a hub or a link, each from one source. It fits because the questions are many-to-many (a paper has many concepts, a concept many papers), the business key differs from the technical one (a preprint and the published version are two files and one paper), and the same link comes from several sources (keywords given by the authors, concepts extracted by a model) that are kept side by side and compared.

Not everything is a hub. A hub has a key of its own, exists apart from documents, and is searched by:

| Candidate | Becomes | Why |
|---|---|---|
| document (paper, book, …) | hub `hub_document` | DOI, ISBN; one document can come in several files |
| author | hub `hub_author` | writes many documents |
| topic, domain, exercise, muscle ("sport", "squat") | one hub `hub_concept`, `concept_type` in its satellite | things of one kind that differ by type and by place in a hierarchy; a hub per type would need a migration per new domain and split the hierarchy across tables |
| kind of source (article, book, video) | an attribute of the document | no life of its own |

Documents and authors are explicit hubs, since they have structured attributes and stable keys. Everything extracted from text goes into one concept graph with few, typed relations: `broader` (hierarchy) and aliases, as in the SKOS thesaurus standard. A fully generic graph (`hub_entity(type)` + `link(from, relation, to)`) is not the goal: attributes lose their types and the graph becomes too tangled to rank by.

```
hub_author ──link_document_author── hub_document ──link_document_file── files ── doc_chunks
                                        │
                               link_document_concept   (source, weight, quote)
                                        │
                                   hub_concept ── concept_aliases ("присед", "приседания")
                                        │
                               link_concept_broader    back squat → squat → strength training → sport
```

## Flow

```
upload ─▶ workflow Index(file_id)                            stream, in llm_worker
            ├ index_file        chunks + embeddings (as today)
            └ read_document     PDF/text → document, authors, metadata          ← S5

POST /api/concept-builds ─▶ workflow BuildConcepts ─▶ Job build_concepts   batch, in mle_models ← S6
            every document: title + abstract + keywords → concepts
            all concepts at once: merge duplicates → broader → concept_ancestors
            one run_id for the whole graph; the marker last switches readers to it

message ─▶ workflow Chat
            ├ route             answer a question | find documents on a topic   ← S7
            ├ retrieve → generate → record           (as today)
            └ find_documents    topic → concepts → descendants → documents      ← S7
```

## Decisions

1. **Links and satellites are outputs of runs; hubs are views of their keys.** Links and satellites are written under the `run_id` of the run that produced them, with a marker in `runs`, and read through `_current` views (principles 4 and 5, README rules 5 and 11). For documents that is the latest committed `read_document` run per file; for concepts it is the latest committed graph as a whole (decision 4). A hub is the set of keys of the current runs, a view such as `SELECT DISTINCT concept_id FROM sat_concept_current`, not a table of its own: documents, authors and concepts are found by a model, so a key written by an attempt that failed, or by a graph that merged it away, must not stay visible. The keys are natural (decision 2), so a hub keeps the stable identity Data Vault wants from it without a physical table. Reference data with its own key, outside runs, is only what comes from outside the platform: `files` and the hand-written `concept_roots`.
2. **Keys.**
   - `document_id`: `doi:<lowercase DOI>` when the text has a DOI, `isbn:<digits>` for a book with an ISBN, otherwise `file:<file_id>`. A DOI is found by a regular expression, not by the model.
   - `author_id`: `orcid:<id>` when present, otherwise `name:<surname> <initials>`, normalized. Merging two keys of one person is later.
   - `concept_id`: sha256 of the canonical English term, lowercased. The model gives the canonical term at extraction ("приседания" → `squat`); the original words go to `concept_aliases`.
3. **PDF is read by `pypdf`, metadata by the chat model.** The first two pages go to the LLM with a JSON schema (Ollama structured output): title, authors, year, kind, abstract, keywords. No new service in the cluster. GROBID (structure of scientific PDFs) is better for two-column layouts, but it is a Java service with 1–2 GB of memory more; it is taken when the quality of the metadata turns out to be the problem.
4. **The concept graph is built by a Job over the whole corpus, not at upload.** Merging duplicates and placing a concept in the hierarchy need every concept at once: grown document by document, the tree depends on the order of uploads ("barbell squat" next to "squat" instead of under it). Building it is the work of a model over a dataset, so a Job (principle 3), started by a workflow `BuildConcepts` on the `batch` queue through `run_job`. The whole graph is one run: readers switch to a new graph at once when its marker is committed, two graphs can be compared, and going back is the previous run. Until the next build a new document is not in the graph; `find_documents` still finds it by the closeness of its abstract.
5. **What the Job does.**
   - Concepts of every document from its title, abstract and keywords: one LLM call per document, not per chunk; the abstract already names what the paper is about. Keywords given by the authors are links with `source='author'`, extracted ones `source='llm:<model>'`.
   - Concepts close by embedding of their canonical terms are merged into one, the others kept as aliases.
   - The hierarchy: a short list of root domains (sport, medicine, nutrition, …) is reference data, the table `concept_roots`, written by a migration and read by the Job; the model places the concepts under the roots and under each other, and a concept may have several parents, as SKOS allows.
   - The closure `concept_ancestors` (concept, ancestor, depth) is computed by the Job from `broader` and written with the graph, so "everything under sport" is one join and no recursion at query time.
   - The model code (prompts, merging, the hierarchy) is a library that gets the LLM as a function and reads no database (principle 2); the platform layer of the Job reads `sat_document_current` and writes the graph. The LLM adapter is copied from llm_worker (principle 9).
6. **A topic in a question is resolved through concepts, not chunks.** The question's topic is embedded by `bge-m3` (multilingual: "присед" lands next to "squat"), the nearest concepts above a threshold are taken with their descendants, and the documents linked to them are ranked: the link weight (author keyword 1.0, extracted 0.7), lower for a deeper descendant, plus the closeness of the abstract to the topic. The weights are parameters of the step, set by the workflow (principle 7), and tuned later on an evaluation set.
7. **`Chat` decides what a message is.** An activity `route` asks the chat model, with a JSON schema, whether the message is a question to answer from the documents or a request for documents on a topic; the workflow branches on the answer. An agent with tools stays later.
8. **Compatibility (principle 6).** The new step `read_document` in `Index` and the new steps in `Chat` go behind `workflow.patched()`, so workflows started on the previous code replay. `BuildConcepts` is a new workflow. New tables only: `files`, `doc_chunks` and `chat_messages` do not change. `files` already plays the hub of files and keeps its name.

## Tables

| Table | Kind | Key | Columns |
|---|---|---|---|
| `hub_document` | hub, view over `link_document_file_current` | `document_id` | |
| `hub_author` | hub, view over `link_document_author_current` | `author_id` | |
| `hub_concept` | hub, view over `sat_concept_current` | `concept_id` | `term` |
| `concept_roots` | reference data, migration | `concept_id` | `term`, `concept_type` |
| `sat_document` | satellite, run `kind='document'` | `run_id` | `document_id`, `title`, `year`, `kind`, `abstract`, `abstract_embedding`, `embed_model` |
| `link_document_file` | link, same run | `run_id` | `document_id`, `file_id` |
| `link_document_author` | link, same run | `run_id` | `document_id`, `author_id`, `position` |
| `sat_concept` | satellite, Job run `kind='concepts'` | `run_id` | `concept_id`, `term` (canonical, en), `concept_type`, `embedding`, `embed_model` |
| `concept_aliases` | satellite, same run | `run_id` | `concept_id`, `alias`, `lang` |
| `link_document_concept` | link, same run | `run_id` | `document_id`, `document_run_id`, `concept_id`, `source`, `weight`, `quote` |
| `link_concept_broader` | link, same run | `run_id` | `concept_id`, `broader_id` |
| `concept_ancestors` | closure, same run | `run_id` | `concept_id`, `ancestor_id`, `depth` |

Markers: `kind='document'` with parents `file_id`, `chat_model`; `kind='concepts'` with parents `chat_model`, `embed_model`, and the documents it read are in `link_document_concept.document_run_id`. The concept tables are ordered by the column they are searched by (`concept_id`, `document_id`) rather than by `run_id`; a retried Job deletes its partial rows by `run_id` before writing them again.

## Slices

Each slice ends working end to end. After each one: 2–3 check questions, and the critical part is written or read line by line by the platform owner.

| # | Slice | Built | Critical part | Done when |
|---|---|---|---|---|
| S5 | Documents | `.pdf` in `rag` (`pypdf`), upload accepts PDF in be_service and chat_service; migrations of `sat_document`, `link_document_file`, `link_document_author`, their views and the hub views; activity `read_document` in `Index`; `make documents` | the choice of `document_id` and the idempotent write of the document run and its marker | a preprint and the published PDF of one paper are one document with two files; uploading again duplicates nothing; title, authors, year, abstract and keywords are visible |
| S6 | Concepts | `concept_roots`; `sat_concept`, `concept_aliases`, `link_document_concept`, `link_concept_broader`, `concept_ancestors`, their views and `hub_concept`; library and Job `build_concepts` in mle_models; workflow `BuildConcepts` in batch_worker; platform API `POST /api/concept-builds` (be_service makes the `run_id`); `make concepts` calls it and prints the tree | canonical terms and `concept_id`; the Job's write of the whole graph and its marker | after 10–20 papers the tree reads sensibly by eye (sport → strength training → squat → back squat), "присед" resolves to `squat`, and a second build replaces the first at once |
| S7 | Finding documents | activity `route` and the branch in `Chat`; activity `find_documents` with ranking weights from the workflow | the Update handler with the new branch behind `patched` | "покажи статьи по теме присед" in the chat lists papers with title, year and the quote that matched; a question about their content is still answered from chunks |

**Since the login stage** ([auth-plan.md](auth-plan.md), decision 15): every file is a user's, and readers see data only through views of what a user owns. The concept graph is built over the whole corpus and a concept such as a character of a novel tells what a book is, so its readers see a concept only through documents the user owns; settling how is part of S6, before it is built.

## Later

An incremental build that reuses the extraction of unchanged documents from the previous graph · `BuildConcepts` on a schedule or after a batch of uploads · merging duplicate authors (entity resolution) · concepts mentioned in chunks, not only in the abstract · GROBID for scientific PDFs · a second metadata satellite from Crossref by DOI · a controlled vocabulary (MeSH) as a source of concepts · a summary tree per document (RAPTOR) and map-reduce over the documents found ("what do these papers say about squat depth") · the concept graph in Postgres once people curate it by hand · searches and the documents chosen from them as training data for a reranker, the next model stage · document titles instead of file names in the sources of an answer · an agent with tools in place of `route`.

## Memory budget

No new service: `pypdf` is a small library in llm_worker, and reading a document's metadata is one LLM call of seconds on the GPU. The Job `build_concepts` holds the documents' abstracts and the concept embeddings, megabytes for thousands of documents, and makes one LLM call per document: tens of minutes for a thousand. It does not run together with MNIST training. The budget of [llm-plan.md](llm-plan.md#memory-budget) holds. `num_ctx` of the chat model has to fit two pages of a PDF and the JSON schema: set it explicitly, otherwise Ollama cuts the prompt silently.
