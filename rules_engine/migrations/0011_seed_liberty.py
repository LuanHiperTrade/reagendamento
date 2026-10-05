from django.db import migrations

# Na tela de edição da rota do Puzzle a marca aparece como "LIBERTY";
# na planilha, nas rotas fixas e no cadastro das atividades é "LIBERTY PLAST".
EQUIVALENCIAS = [
    ("LIBERTY", "LIBERTY PLAST"),
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
        ("rules_engine", "0010_atividade_industria"),
    ]

    operations = [
        migrations.RunPython(cadastrar, remover),
    ]
