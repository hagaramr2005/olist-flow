.PHONY: install run test seed data measure
install:
	pip install -r requirements.txt
run:
	python -m app.main
test:
	python -m pytest -q
seed:  ## needs the server running; logs in as admin and seeds 80 demo orders
	python scripts/seed_demo.py

data:  ## rebuild the real-data calibration artifact: make data DATA=path/to/olist_csvs
	pip install -r requirements-data.txt
	python scripts/build_calibration.py --data-dir $(DATA)
measure:  ## reproduce the README "Measured results"
	python scripts/measure.py
