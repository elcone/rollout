from jinja2 import Environment, PackageLoader

sql_templates = Environment(
    loader=PackageLoader("rollout", "templates/sql"),
    autoescape=False,
)
