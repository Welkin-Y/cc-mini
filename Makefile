.PHONY: build run bash test jupyter

build:
	docker-compose build

run:
	docker-compose run --rm cc-mini

bash:
	docker-compose run --rm cc-mini bash

test:
	docker-compose run --rm cc-mini pytest tests -v -k "not integration"

jupyter:
	docker-compose run --rm --service-ports cc-mini jupyter lab --ip 0.0.0.0 --port 8888 --no-browser --allow-root --ServerApp.allow_remote_access=False --IdentityProvider.token='' --PasswordIdentityProvider.hashed_password='' /workspace/notebooks/cc_mini.ipynb
