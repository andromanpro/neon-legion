.PHONY: demo

PYTHON ?= python

demo:
	$(PYTHON) tools/gen-fake-events.py --days 7
	$(PYTHON) backend/server.py --snapshot-once --snapshot-path dashboard/snapshot.json
	@echo Open dashboard/index.html in your browser.
