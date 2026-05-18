archive:
	python3 archive.py

archive-refresh:
	python3 archive.py --refresh

run:
	python3 app.py

freeze:
	python3 freeze.py

deploy:
	ghp-import -n -p -f build/

.PHONY: archive archive-refresh run freeze deploy
