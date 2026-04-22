.PHONY: build run bash test

build:
	docker-compose build

run:
	docker-compose run --rm cc-mini

bash:
	docker-compose run --rm cc-mini bash

test:
	docker-compose run --rm cc-mini pytest tests -v -k "not integration"
