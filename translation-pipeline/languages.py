"""
languages.py — fixed sentence templates for the patient sheet, one set per language.

Every sentence that carries a dose (how much, when, how often, food, how long, limits) is built
from these templates, so no model ever re-words a dose. Only short free text (what a medicine is
for, the illness, the doctor's own notes, apologies) is translated by a model, and checked.

To be checked by a native-speaking health professional for each language before real use.

Each language gives:
    name, native, dir ("ltr" / "rtl"), model_name (what the translation model is asked for)
    headings and fixed lines, time-of-day words, frequency phrases, food phrases, units,
    forms (pill, capsule, sachet, puff, drop, lozenge, ml, spoon) and builders:
        dose(n, form, name)  -> "2 comprimés de Doliprane"
        amount(n, form)      -> "2 comprimés"
        line(take, dose, when, extras) -> the first sentence of a medicine
"""

from __future__ import annotations

FORMS = ("pill", "capsule", "sachet", "puff", "drop", "lozenge", "ml", "spoon")
TIMES = ("morning", "noon", "evening", "bedtime")
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _plural(n: str, forms: tuple) -> str:
    return forms[0] if n in ("1", "1/2", "½") else forms[1]


def _latin_line(sep=", "):
    """Most languages: "<Take> <dose> <when>, <extra>, <extra>."."""
    def line(take, dose, when, extras):
        first = " ".join(p for p in (take, dose, when) if p)
        return sep.join([first, *extras])
    return line


# --------------------------------------------------------------------------- #
# French
# --------------------------------------------------------------------------- #

def _fr_de(name: str) -> str:
    return "d’" if name[:1].lower() in "aeiouyhéèêâîôû" else "de "


FR = {
    "name": "French", "native": "Français", "dir": "ltr", "model_name": "French",
    "Your illness": "Votre maladie", "Your medicines": "Vos médicaments", "Also": "Votre médecin dit aussi",
    "When to take them each day": "Quand les prendre chaque jour",
    "Sorry, I could not understand everything": "Désolé, je n’ai pas tout compris",
    "intro": "Voici comment prendre vos médicaments.",
    "disclaimer": "Si vous avez un doute, demandez à votre médecin ou à votre pharmacien.",
    "ask_about_this": "Demandez à votre médecin ou à votre pharmacien.",
    "take": "Prenez", "take_it": "Prenez ce médicament",
    "time": {"morning": "le matin", "noon": "à midi", "evening": "le soir", "bedtime": "au coucher"},
    "slot": {"morning": "Matin", "noon": "Midi", "evening": "Soir", "bedtime": "Au coucher"},
    "if_needed_slot": "Seulement si besoin", "and": " et ", "sep": ", ",
    "n_a_day": lambda n: "une fois par jour" if n == 1 else f"{n} fois par jour",
    "n_a_week": lambda n: "une fois par semaine" if n == 1 else f"{n} fois par semaine",
    "on_day": lambda d: f"le {d}",
    "days": dict(zip(DAYS, ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"))),
    "range_a_day": lambda lo, hi: f"{lo} à {hi} fois par jour",
    "single_dose": "une seule fois (une seule prise)",
    "only_when": lambda need: f"seulement en cas de {need}", "only_when_needed": "seulement si besoin",
    "up_to": lambda n: "au maximum une fois par jour" if n == 1 else f"jusqu’à {n} fois par jour",
    "food": {"with food": "pendant le repas", "before food": "avant le repas", "after food": "après le repas",
             "empty stomach": "à jeun, avant de manger"},
    "for_duration": lambda d: f"pendant {d}",
    "units": {"day": ("jour", "jours"), "week": ("semaine", "semaines"), "month": ("mois", "mois")},
    "wait_hours": lambda h: f"Attendez au moins {h} heures avant d’en reprendre.",
    "max_day": lambda m: f"Ne prenez pas plus de {m} par jour.",
    "long_term": "C’est un traitement au long cours.",
    "forms": {"pill": ("comprimé", "comprimés"), "capsule": ("gélule", "gélules"), "sachet": ("sachet", "sachets"),
              "puff": ("bouffée", "bouffées"), "drop": ("goutte", "gouttes"), "lozenge": ("pastille", "pastilles"),
              "ml": ("ml", "ml"), "spoon": ("cuillère", "cuillères")},
}
FR["dose"] = lambda n, form, name: f"{n} {_plural(n, FR['forms'][form])} {_fr_de(name)}{name}"
FR["amount"] = lambda n, form: f"{n} {_plural(n, FR['forms'][form])}"
FR["line"] = _latin_line()

# --------------------------------------------------------------------------- #
# Spanish
# --------------------------------------------------------------------------- #

ES = {
    "name": "Spanish", "native": "Español", "dir": "ltr", "model_name": "Spanish",
    "Your illness": "Su enfermedad", "Your medicines": "Sus medicamentos", "Also": "Su médico también dice",
    "When to take them each day": "Cuándo tomarlos cada día",
    "Sorry, I could not understand everything": "Lo siento, no he entendido todo",
    "intro": "Así debe tomar sus medicamentos.",
    "disclaimer": "Si tiene alguna duda, pregunte a su médico o farmacéutico.",
    "ask_about_this": "Pregunte a su médico o farmacéutico.",
    "take": "Tome", "take_it": "Tome este medicamento",
    "time": {"morning": "por la mañana", "noon": "al mediodía", "evening": "por la noche", "bedtime": "al acostarse"},
    "slot": {"morning": "Mañana", "noon": "Mediodía", "evening": "Noche", "bedtime": "Al acostarse"},
    "if_needed_slot": "Solo si lo necesita", "and": " y ", "sep": ", ",
    "n_a_day": lambda n: "una vez al día" if n == 1 else f"{n} veces al día",
    "n_a_week": lambda n: "una vez a la semana" if n == 1 else f"{n} veces a la semana",
    "on_day": lambda d: f"el {d}",
    "days": dict(zip(DAYS, ("lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"))),
    "range_a_day": lambda lo, hi: f"de {lo} a {hi} veces al día",
    "single_dose": "solo una vez (una única dosis)",
    "only_when": lambda need: f"solo si tiene {need}", "only_when_needed": "solo si lo necesita",
    "up_to": lambda n: "como máximo una vez al día" if n == 1 else f"hasta {n} veces al día",
    "food": {"with food": "con la comida", "before food": "antes de comer", "after food": "después de comer",
             "empty stomach": "en ayunas, antes de comer nada"},
    "for_duration": lambda d: f"durante {d}",
    "units": {"day": ("día", "días"), "week": ("semana", "semanas"), "month": ("mes", "meses")},
    "wait_hours": lambda h: f"Espere al menos {h} horas antes de tomar otra dosis.",
    "max_day": lambda m: f"No tome más de {m} al día.",
    "long_term": "Es un tratamiento de larga duración.",
    "forms": {"pill": ("comprimido", "comprimidos"), "capsule": ("cápsula", "cápsulas"), "sachet": ("sobre", "sobres"),
              "puff": ("inhalación", "inhalaciones"), "drop": ("gota", "gotas"), "lozenge": ("pastilla", "pastillas"),
              "ml": ("ml", "ml"), "spoon": ("cucharada", "cucharadas")},
}
ES["dose"] = lambda n, form, name: f"{n} {_plural(n, ES['forms'][form])} de {name}"
ES["amount"] = lambda n, form: f"{n} {_plural(n, ES['forms'][form])}"
ES["line"] = _latin_line()

# --------------------------------------------------------------------------- #
# Portuguese
# --------------------------------------------------------------------------- #

PT = {
    "name": "Portuguese", "native": "Português", "dir": "ltr", "model_name": "Portuguese",
    "Your illness": "A sua doença", "Your medicines": "Os seus medicamentos", "Also": "O seu médico também diz",
    "When to take them each day": "Quando os tomar todos os dias",
    "Sorry, I could not understand everything": "Desculpe, não percebi tudo",
    "intro": "Eis como tomar os seus medicamentos.",
    "disclaimer": "Se tiver alguma dúvida, pergunte ao seu médico ou farmacêutico.",
    "ask_about_this": "Pergunte ao seu médico ou farmacêutico.",
    "take": "Tome", "take_it": "Tome este medicamento",
    "time": {"morning": "de manhã", "noon": "ao meio-dia", "evening": "à noite", "bedtime": "ao deitar"},
    "slot": {"morning": "Manhã", "noon": "Meio-dia", "evening": "Noite", "bedtime": "Ao deitar"},
    "if_needed_slot": "Apenas se precisar", "and": " e ", "sep": ", ",
    "n_a_day": lambda n: "uma vez por dia" if n == 1 else f"{n} vezes por dia",
    "n_a_week": lambda n: "uma vez por semana" if n == 1 else f"{n} vezes por semana",
    "on_day": lambda d: f"{'no' if d in ('sábado', 'domingo') else 'na'} {d}",
    "days": dict(zip(DAYS, ("segunda-feira", "terça-feira", "quarta-feira", "quinta-feira", "sexta-feira",
                            "sábado", "domingo"))),
    "range_a_day": lambda lo, hi: f"{lo} a {hi} vezes por dia",
    "single_dose": "apenas uma vez (uma dose única)",
    "only_when": lambda need: f"apenas se tiver {need}", "only_when_needed": "apenas se precisar",
    "up_to": lambda n: "no máximo uma vez por dia" if n == 1 else f"até {n} vezes por dia",
    "food": {"with food": "com a refeição", "before food": "antes de comer", "after food": "depois de comer",
             "empty stomach": "em jejum, antes de comer"},
    "for_duration": lambda d: f"durante {d}",
    "units": {"day": ("dia", "dias"), "week": ("semana", "semanas"), "month": ("mês", "meses")},
    "wait_hours": lambda h: f"Espere pelo menos {h} horas antes de tomar outra dose.",
    "max_day": lambda m: f"Não tome mais de {m} por dia.",
    "long_term": "É um tratamento de longa duração.",
    "forms": {"pill": ("comprimido", "comprimidos"), "capsule": ("cápsula", "cápsulas"), "sachet": ("saqueta", "saquetas"),
              "puff": ("inalação", "inalações"), "drop": ("gota", "gotas"), "lozenge": ("pastilha", "pastilhas"),
              "ml": ("ml", "ml"), "spoon": ("colher", "colheres")},
}
PT["dose"] = lambda n, form, name: f"{n} {_plural(n, PT['forms'][form])} de {name}"
PT["amount"] = lambda n, form: f"{n} {_plural(n, PT['forms'][form])}"
PT["line"] = _latin_line()

# --------------------------------------------------------------------------- #
# Italian
# --------------------------------------------------------------------------- #

IT = {
    "name": "Italian", "native": "Italiano", "dir": "ltr", "model_name": "Italian",
    "Your illness": "La sua malattia", "Your medicines": "I suoi farmaci", "Also": "Il medico dice anche",
    "When to take them each day": "Quando prenderli ogni giorno",
    "Sorry, I could not understand everything": "Mi scusi, non ho capito tutto",
    "intro": "Ecco come prendere i suoi farmaci.",
    "disclaimer": "Se ha dei dubbi, chieda al medico o al farmacista.",
    "ask_about_this": "Chieda al medico o al farmacista.",
    "take": "Prenda", "take_it": "Prenda questo farmaco",
    "time": {"morning": "al mattino", "noon": "a mezzogiorno", "evening": "alla sera", "bedtime": "prima di dormire"},
    "slot": {"morning": "Mattino", "noon": "Mezzogiorno", "evening": "Sera", "bedtime": "Prima di dormire"},
    "if_needed_slot": "Solo se necessario", "and": " e ", "sep": ", ",
    "n_a_day": lambda n: "una volta al giorno" if n == 1 else f"{n} volte al giorno",
    "n_a_week": lambda n: "una volta alla settimana" if n == 1 else f"{n} volte alla settimana",
    "on_day": lambda d: f"{'la' if d == 'domenica' else 'il'} {d}",
    "days": dict(zip(DAYS, ("lunedì", "martedì", "mercoledì", "giovedì", "venerdì", "sabato", "domenica"))),
    "range_a_day": lambda lo, hi: f"da {lo} a {hi} volte al giorno",
    "single_dose": "solo una volta (una dose unica)",
    "only_when": lambda need: f"solo in caso di {need}", "only_when_needed": "solo se necessario",
    "up_to": lambda n: "al massimo una volta al giorno" if n == 1 else f"fino a {n} volte al giorno",
    "food": {"with food": "durante il pasto", "before food": "prima di mangiare", "after food": "dopo aver mangiato",
             "empty stomach": "a stomaco vuoto, prima di mangiare"},
    "for_duration": lambda d: f"per {d}",
    "units": {"day": ("giorno", "giorni"), "week": ("settimana", "settimane"), "month": ("mese", "mesi")},
    "wait_hours": lambda h: f"Aspetti almeno {h} ore prima di prenderne un’altra dose.",
    "max_day": lambda m: f"Non prenda più di {m} al giorno.",
    "long_term": "È una terapia a lungo termine.",
    "forms": {"pill": ("compressa", "compresse"), "capsule": ("capsula", "capsule"), "sachet": ("bustina", "bustine"),
              "puff": ("inalazione", "inalazioni"), "drop": ("goccia", "gocce"), "lozenge": ("pastiglia", "pastiglie"),
              "ml": ("ml", "ml"), "spoon": ("cucchiaio", "cucchiai")},
}
IT["dose"] = lambda n, form, name: f"{n} {_plural(n, IT['forms'][form])} di {name}"
IT["amount"] = lambda n, form: f"{n} {_plural(n, IT['forms'][form])}"
IT["line"] = _latin_line()

# --------------------------------------------------------------------------- #
# German
# --------------------------------------------------------------------------- #

DE = {
    "name": "German", "native": "Deutsch", "dir": "ltr", "model_name": "German",
    "Your illness": "Ihre Erkrankung", "Your medicines": "Ihre Medikamente", "Also": "Ihr Arzt sagt außerdem",
    "When to take them each day": "Wann Sie sie jeden Tag nehmen",
    "Sorry, I could not understand everything": "Entschuldigung, ich habe nicht alles verstanden",
    "intro": "So nehmen Sie Ihre Medikamente.",
    "disclaimer": "Wenn Sie unsicher sind, fragen Sie Ihren Arzt oder Apotheker.",
    "ask_about_this": "Fragen Sie Ihren Arzt oder Apotheker.",
    "take": "Nehmen Sie", "take_it": "Nehmen Sie dieses Medikament",
    "time": {"morning": "morgens", "noon": "mittags", "evening": "abends", "bedtime": "vor dem Schlafengehen"},
    "slot": {"morning": "Morgens", "noon": "Mittags", "evening": "Abends", "bedtime": "Vor dem Schlafengehen"},
    "if_needed_slot": "Nur bei Bedarf", "and": " und ", "sep": ", ",
    "n_a_day": lambda n: "einmal täglich" if n == 1 else f"{n}-mal täglich",
    "n_a_week": lambda n: "einmal pro Woche" if n == 1 else f"{n}-mal pro Woche",
    "on_day": lambda d: f"am {d}",
    "days": dict(zip(DAYS, ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"))),
    "range_a_day": lambda lo, hi: f"{lo}- bis {hi}-mal täglich",
    "single_dose": "nur einmal (eine einzige Dosis)",
    "only_when": lambda need: f"nur bei {need}", "only_when_needed": "nur bei Bedarf",
    "up_to": lambda n: "höchstens einmal täglich" if n == 1 else f"bis zu {n}-mal täglich",
    "food": {"with food": "zum Essen", "before food": "vor dem Essen", "after food": "nach dem Essen",
             "empty stomach": "auf nüchternen Magen, vor dem Essen"},
    "for_duration": lambda d: f"{d} lang",
    "units": {"day": ("Tag", "Tage"), "week": ("Woche", "Wochen"), "month": ("Monat", "Monate")},
    "wait_hours": lambda h: f"Warten Sie mindestens {h} Stunden bis zur nächsten Einnahme.",
    "max_day": lambda m: f"Nehmen Sie nicht mehr als {m} pro Tag.",
    "long_term": "Dies ist eine Langzeitbehandlung.",
    "forms": {"pill": ("Tablette", "Tabletten"), "capsule": ("Kapsel", "Kapseln"), "sachet": ("Beutel", "Beutel"),
              "puff": ("Hub", "Hübe"), "drop": ("Tropfen", "Tropfen"), "lozenge": ("Lutschtablette", "Lutschtabletten"),
              "ml": ("ml", "ml"), "spoon": ("Löffel", "Löffel")},
}
DE["dose"] = lambda n, form, name: f"{n} {_plural(n, DE['forms'][form])} {name}"
DE["amount"] = lambda n, form: f"{n} {_plural(n, DE['forms'][form])}"
DE["line"] = _latin_line()

# --------------------------------------------------------------------------- #
# Dutch
# --------------------------------------------------------------------------- #

NL = {
    "name": "Dutch", "native": "Nederlands", "dir": "ltr", "model_name": "Dutch",
    "Your illness": "Uw ziekte", "Your medicines": "Uw medicijnen", "Also": "Uw arts zegt ook",
    "When to take them each day": "Wanneer u ze elke dag neemt",
    "Sorry, I could not understand everything": "Sorry, ik heb niet alles begrepen",
    "intro": "Zo neemt u uw medicijnen.",
    "disclaimer": "Als u twijfelt, vraag het aan uw arts of apotheker.",
    "ask_about_this": "Vraag het aan uw arts of apotheker.",
    "take": "Neem", "take_it": "Neem dit medicijn",
    "time": {"morning": "’s ochtends", "noon": "’s middags", "evening": "’s avonds", "bedtime": "voor het slapengaan"},
    "slot": {"morning": "Ochtend", "noon": "Middag", "evening": "Avond", "bedtime": "Voor het slapengaan"},
    "if_needed_slot": "Alleen als het nodig is", "and": " en ", "sep": ", ",
    "n_a_day": lambda n: "één keer per dag" if n == 1 else f"{n} keer per dag",
    "n_a_week": lambda n: "één keer per week" if n == 1 else f"{n} keer per week",
    "on_day": lambda d: f"op {d}",
    "days": dict(zip(DAYS, ("maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"))),
    "range_a_day": lambda lo, hi: f"{lo} tot {hi} keer per dag",
    "single_dose": "maar één keer (één enkele dosis)",
    "only_when": lambda need: f"alleen bij {need}", "only_when_needed": "alleen als het nodig is",
    "up_to": lambda n: "hoogstens één keer per dag" if n == 1 else f"tot {n} keer per dag",
    "food": {"with food": "met eten", "before food": "voor het eten", "after food": "na het eten",
             "empty stomach": "op een lege maag, voor het eten"},
    "for_duration": lambda d: f"gedurende {d}",
    "units": {"day": ("dag", "dagen"), "week": ("week", "weken"), "month": ("maand", "maanden")},
    "wait_hours": lambda h: f"Wacht minstens {h} uur voordat u er nog een neemt.",
    "max_day": lambda m: f"Neem niet meer dan {m} per dag.",
    "long_term": "Dit is een langdurige behandeling.",
    "forms": {"pill": ("tablet", "tabletten"), "capsule": ("capsule", "capsules"), "sachet": ("zakje", "zakjes"),
              "puff": ("pufje", "pufjes"), "drop": ("druppel", "druppels"), "lozenge": ("zuigtablet", "zuigtabletten"),
              "ml": ("ml", "ml"), "spoon": ("lepel", "lepels")},
}
NL["dose"] = lambda n, form, name: f"{n} {_plural(n, NL['forms'][form])} {name}"
NL["amount"] = lambda n, form: f"{n} {_plural(n, NL['forms'][form])}"
NL["line"] = _latin_line()

# --------------------------------------------------------------------------- #
# Turkish (verb last: "1 tablet Doliprane alın: sabah ve akşam, yemekle birlikte, 8 gün boyunca.")
# --------------------------------------------------------------------------- #

TR = {
    "name": "Turkish", "native": "Türkçe", "dir": "ltr", "model_name": "Turkish",
    "Your illness": "Hastalığınız", "Your medicines": "İlaçlarınız", "Also": "Doktorunuz ayrıca şunu söylüyor",
    "When to take them each day": "Her gün ne zaman alınacak",
    "Sorry, I could not understand everything": "Üzgünüm, her şeyi anlayamadım",
    "intro": "İlaçlarınızı şöyle alın.",
    "disclaimer": "Emin olmadığınız bir şey varsa doktorunuza veya eczacınıza sorun.",
    "ask_about_this": "Doktorunuza veya eczacınıza sorun.",
    "take": "alın", "take_it": "Bu ilacı alın",
    "time": {"morning": "sabah", "noon": "öğlen", "evening": "akşam", "bedtime": "yatmadan önce"},
    "slot": {"morning": "Sabah", "noon": "Öğlen", "evening": "Akşam", "bedtime": "Yatmadan önce"},
    "if_needed_slot": "Yalnızca gerektiğinde", "and": " ve ", "sep": ", ",
    "n_a_day": lambda n: "günde bir kez" if n == 1 else f"günde {n} kez",
    "n_a_week": lambda n: "haftada bir kez" if n == 1 else f"haftada {n} kez",
    "on_day": lambda d: f"{d} günü",
    "days": dict(zip(DAYS, ("Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"))),
    "range_a_day": lambda lo, hi: f"günde {lo} ile {hi} kez",
    "single_dose": "yalnızca bir kez (tek doz)",
    "only_when": lambda need: f"yalnızca {need} olduğunda", "only_when_needed": "yalnızca gerektiğinde",
    "up_to": lambda n: "günde en fazla bir kez" if n == 1 else f"günde en fazla {n} kez",
    "food": {"with food": "yemekle birlikte", "before food": "yemekten önce", "after food": "yemekten sonra",
             "empty stomach": "aç karnına, bir şey yemeden önce"},
    "for_duration": lambda d: f"{d} boyunca",
    "units": {"day": ("gün", "gün"), "week": ("hafta", "hafta"), "month": ("ay", "ay")},
    "wait_hours": lambda h: f"Bir sonrakini almadan önce en az {h} saat bekleyin.",
    "max_day": lambda m: f"Günde en fazla {m} alın.",
    "long_term": "Bu uzun süreli bir tedavidir.",
    # Turkish nouns stay singular after a number.
    "forms": {"pill": ("tablet", "tablet"), "capsule": ("kapsül", "kapsül"), "sachet": ("saşe", "saşe"),
              "puff": ("püskürtme", "püskürtme"), "drop": ("damla", "damla"), "lozenge": ("pastil", "pastil"),
              "ml": ("ml", "ml"), "spoon": ("kaşık", "kaşık")},
}
TR["dose"] = lambda n, form, name: f"{n} {TR['forms'][form][0]} {name}"
TR["amount"] = lambda n, form: f"{n} {TR['forms'][form][0]}"


def _tr_line(take, dose, when, extras):
    head = f"{dose} {take}" if dose else "Bu ilacı alın"
    rest = [p for p in (when, *extras) if p]
    return head + (": " + ", ".join(rest) if rest else "")


TR["line"] = _tr_line

# --------------------------------------------------------------------------- #
# Modern Standard Arabic
# --------------------------------------------------------------------------- #

def _ar_count(n: str, forms: tuple) -> str:
    """forms = (singular, plural, dual, "one" word): 1 -> "قرص واحد", 2 -> "قرصان", 3-10 -> "3 أقراص", 11+ -> "12 قرص"."""
    singular, plural, dual, one = forms
    if n == "1":
        return f"{singular} {one}"
    if n == "2":
        return dual
    if n.isdigit() and 3 <= int(n) <= 10:
        return f"{n} {plural}"
    return f"{n} {singular}"


AR = {
    "name": "Arabic (Modern Standard)", "native": "العربية الفصحى", "dir": "rtl",
    "model_name": "Modern Standard Arabic (الفصحى), in Arabic script",
    "Your illness": "مرضك", "Your medicines": "أدويتك", "Also": "يقول طبيبك أيضًا",
    "When to take them each day": "متى تأخذها كل يوم",
    "Sorry, I could not understand everything": "عذرًا، لم أفهم كل شيء",
    "intro": "إليك كيف تأخذ أدويتك.",
    "disclaimer": "إذا كان لديك أي شك، اسأل طبيبك أو الصيدلي.",
    "ask_about_this": "اسأل طبيبك أو الصيدلي.",
    "take": "خذ", "take_it": "خذ هذا الدواء",
    "time": {"morning": "صباحًا", "noon": "ظهرًا", "evening": "مساءً", "bedtime": "قبل النوم"},
    "slot": {"morning": "الصباح", "noon": "الظهر", "evening": "المساء", "bedtime": "قبل النوم"},
    "if_needed_slot": "فقط عند الحاجة", "and": " و", "sep": "، ",
    "n_a_day": lambda n: {1: "مرة واحدة في اليوم", 2: "مرتين في اليوم"}.get(n, f"{n} مرات في اليوم"),
    "n_a_week": lambda n: {1: "مرة واحدة في الأسبوع", 2: "مرتين في الأسبوع"}.get(n, f"{n} مرات في الأسبوع"),
    "on_day": lambda d: f"يوم {d}",
    "days": dict(zip(DAYS, ("الإثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"))),
    "range_a_day": lambda lo, hi: f"من {lo} إلى {hi} مرات في اليوم",
    "single_dose": "مرة واحدة فقط (جرعة واحدة)",
    "only_when": lambda need: f"فقط عند {need}", "only_when_needed": "فقط عند الحاجة",
    "up_to": lambda n: {1: "مرة واحدة في اليوم كحد أقصى", 2: "حتى مرتين في اليوم"}.get(n, f"حتى {n} مرات في اليوم"),
    "food": {"with food": "مع الطعام", "before food": "قبل الأكل", "after food": "بعد الأكل",
             "empty stomach": "على معدة فارغة، قبل الأكل"},
    "for_duration": lambda d: f"لمدة {d}",
    "units": {"day": ("يوم", "أيام", "يومين", "واحد"), "week": ("أسبوع", "أسابيع", "أسبوعين", "واحد"),
              "month": ("شهر", "أشهر", "شهرين", "واحد")},
    "wait_hours": lambda h: f"انتظر {h} ساعات على الأقل قبل أن تأخذ جرعة أخرى.",
    "max_day": lambda m: f"لا تأخذ أكثر من {m} في اليوم.",
    "long_term": "هذا علاج طويل الأمد.",
    "forms": {"pill": ("قرص", "أقراص", "قرصين", "واحد"), "capsule": ("كبسولة", "كبسولات", "كبسولتين", "واحدة"),
              "sachet": ("كيس", "أكياس", "كيسين", "واحد"), "puff": ("بخة", "بخات", "بختين", "واحدة"),
              "drop": ("قطرة", "قطرات", "قطرتين", "واحدة"), "lozenge": ("قرص استحلاب", "أقراص استحلاب", "قرصي استحلاب", "واحد"),
              "ml": ("مل", "مل", "2 مل", ""), "spoon": ("ملعقة", "ملاعق", "ملعقتين", "واحدة")},
}
AR["dose"] = lambda n, form, name: f"{_ar_count(n, AR['forms'][form]).strip()} من {name}"
AR["amount"] = lambda n, form: _ar_count(n, AR["forms"][form]).strip()
AR["unit_count"] = lambda n, unit: _ar_count(str(n), AR["units"][unit]).strip()
AR["line"] = _latin_line("، ")

# --------------------------------------------------------------------------- #
# Moroccan Darija (Arabic script) — the original templates, extended
# --------------------------------------------------------------------------- #

_ARY_FORMS = {"pill": ("حبة", "حبات", "جوج حبات"), "capsule": ("حبة", "حبات", "جوج حبات"),
              "sachet": ("صاشي", "صاشيات", "جوج صاشيات"), "puff": ("بخة", "بخات", "جوج بخات"),
              "drop": ("نقطة", "نقط", "جوج نقط"), "lozenge": ("حبة د المص", "حبات د المص", "جوج حبات د المص"),
              "ml": ("مل", "مل", "2 مل"), "spoon": ("معلقة", "معالق", "جوج معالق")}


def _ary_dose(n, form, name):
    singular, plural, two = _ARY_FORMS[form]
    if n == "1":
        return f"{singular} {'واحد' if form in ('sachet', 'ml') else 'وحدة'} ديال {name}"
    if n == "2":
        return f"{two} ديال {name}"
    return f"{n} {plural} ديال {name}"


def _ary_amount(n, form):
    singular, plural, two = _ARY_FORMS[form]
    return two if n == "2" else f"{n} {singular if n in ('1', '1/2', '½') else plural}"


ARY = {
    "name": "Moroccan Darija", "native": "الدارجة", "dir": "rtl",
    "model_name": "Moroccan Darija (الدارجة المغربية) in Arabic script, the everyday spoken Arabic of Morocco "
                  "(for example: خود، ديال، فالصباح، غير إلا، ما تاخدش). NOT Modern Standard Arabic",
    "Your illness": "المرض ديالك", "Your medicines": "الدوا ديالك", "Also": "الطبيب قال ليك تاني",
    "When to take them each day": "إمتى تاخدهم كل نهار",
    "Sorry, I could not understand everything": "سمح ليا، ما فهمتش كلشي",
    "intro": "ها كيفاش تاخد الدوا ديالك.",
    "disclaimer": "إلا كان عندك شي شك، سول الطبيب ولا الفرماسيان.",
    "ask_about_this": "سول الطبيب ولا الفرماسيان.",
    "take": "خود", "take_it": "خود هاد الدوا",
    "time": {"morning": "فالصباح", "noon": "فالظهر", "evening": "فالعشية", "bedtime": "قبل ما تنعس"},
    "slot": {"morning": "الصباح", "noon": "الظهر", "evening": "العشية", "bedtime": "قبل النعاس"},
    "if_needed_slot": "غير إلا احتاجيتي", "and": " و", "sep": "، ",
    "n_a_day": lambda n: {1: "مرة فالنهار", 2: "جوج مرات فالنهار"}.get(n, f"{n} د المرات فالنهار"),
    "n_a_week": lambda n: {1: "مرة فالسيمانة", 2: "جوج مرات فالسيمانة"}.get(n, f"{n} د المرات فالسيمانة"),
    "on_day": lambda d: f"نهار {d}",
    "days": dict(zip(DAYS, ("التنين", "التلات", "لاربع", "الخميس", "الجمعة", "السبت", "الحد"))),
    "range_a_day": lambda lo, hi: f"من {lo} حتى ل{hi} د المرات فالنهار",
    "single_dose": "غير مرة وحدة (دوز واحد)",
    "only_when": lambda need: f"غير إلا كان عندك {need}", "only_when_needed": "غير إلا احتاجيتي",
    "up_to": lambda n: {1: "حتى لمرة فالنهار", 2: "حتى لجوج مرات فالنهار"}.get(n, f"حتى ل{n} د المرات فالنهار"),
    "food": {"with food": "مع الماكلة", "before food": "قبل الماكلة", "after food": "من بعد الماكلة",
             "empty stomach": "على الريق، قبل ما تاكل والو"},
    "for_duration": lambda d: f"لمدة {d}",
    "units": {"day": ("نهار", "أيام"), "week": ("سيمانة", "سيمانات"), "month": ("شهر", "شهور")},
    "wait_hours": lambda h: f"تسنى على الأقل {h} د السوايع قبل ما تعاود تاخدو.",
    "max_day": lambda m: f"ما تاخدش كتر من {m} فالنهار.",
    "long_term": "هادا علاج طويل.",
    "forms": _ARY_FORMS, "dose": _ary_dose, "amount": _ary_amount,
    "line": _latin_line("، "),
}

LANGUAGES = {"ary": ARY, "ar": AR, "fr": FR, "es": ES, "pt": PT, "it": IT, "de": DE, "nl": NL, "tr": TR}
