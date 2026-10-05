from django.db import migrations

# Equivalências conhecidas: nas tarefas do Puzzle a marca aparece com outro nome
EQUIVALENCIAS = [
    ("TUCS/FAZENDINHA", "BELA VISTA"),
]


def cadastrar(apps, schema_editor):
    MarcaEquivalente = apps.get_model("rules_engine", "MarcaEquivalente")
    for nome, marca in EQUIVALENCIAS:
        MarcaEquivalente.objects.get_or_create(nome=nome, defaults={"marca": marca})


def remover(apps, schema_editor):
    MarcaEquivalente = apps.get_model("rules_engine", "MarcaEquivalente")
    MarcaEquivalente.objects.filter(nome__in=[nome for nome, _ in EQUIVALENCIAS]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("rules_engine", "0008_marca_equivalente"),
    ]

    operations = [
        migrations.RunPython(cadastrar, remover),
    ]
