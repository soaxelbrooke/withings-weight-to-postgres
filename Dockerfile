FROM python:3.10-buster
WORKDIR /app
COPY Pipfile* ./
RUN pip install pipenv && pipenv install
COPY main.py ./
CMD pipenv run python main.py
