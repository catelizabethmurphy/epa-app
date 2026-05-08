fetch:
	python3 fetch.py

fetch-text:
	python3 fetch_text.py

enrich:
	python3 enrich.py

enrich-comments:
	python3 enrich.py --comments

fetch-all: fetch fetch-text enrich

run:
	python3 app.py

freeze:
	python3 freeze.py

deploy:
	ghp-import -n -p -f build/

.PHONY: fetch fetch-text enrich enrich-comments fetch-all run freeze deploy
