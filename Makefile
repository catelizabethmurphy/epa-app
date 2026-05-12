fetch-regs:
	python3 fetch_regs.py

fetch-fr:
	python3 fetch_fr.py

fetch-fr-meta:
	python3 fetch_fr.py --skip-xml

fetch-press:
	python3 fetch_press.py

fetch-agenda:
	python3 fetch_agenda.py

enrich:
	python3 enrich.py

enrich-comments:
	python3 enrich.py --comments

embed:
	python3 embed.py

# Full pipeline: FR (primary) → regs (supplement) → press → agenda → enrich → embed
fetch-all: fetch-fr fetch-regs fetch-press fetch-agenda enrich embed

# Quick update: FR metadata only, skip full-text download and press scraping
fetch-quick: fetch-fr-meta fetch-regs fetch-agenda enrich

# Skip press scraping (for dev when EPA blocks)
fetch-regs-only: fetch-regs fetch-fr enrich embed

run:
	python3 app.py

freeze:
	python3 freeze.py

deploy:
	ghp-import -n -p -f build/

.PHONY: fetch-regs fetch-fr fetch-fr-meta fetch-press fetch-agenda enrich enrich-comments \
        embed fetch-all fetch-quick fetch-regs-only run freeze deploy
