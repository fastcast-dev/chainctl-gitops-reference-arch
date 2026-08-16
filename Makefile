.PHONY: lint plan apply report deps diagram

deps:
	python3 -m pip install --quiet -r requirements.txt

lint:
	./bin/cg-sync lint

plan:
	./bin/cg-sync plan --output text

# Plan in markdown, e.g. for PR comments: make plan-md > plan.md
plan-md:
	./bin/cg-sync plan --output markdown

apply:
	./bin/cg-sync apply

report:
	./bin/cg-sync report --output markdown

# Regenerate the architecture diagram (requires d2: https://d2lang.com)
diagram:
	d2 --layout elk docs/architecture.d2 docs/architecture.svg
