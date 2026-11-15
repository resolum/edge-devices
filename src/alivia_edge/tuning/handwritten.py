"""Casos escritos a mano, independientes de las plantillas del generador: no los usa el entrenamiento.

Miden si el modelo generaliza a frases que no salieron de las mismas plantillas con las que se entrenó. Agrega aquí
(o en un .eval.jsonl propio) las frases reales que el edge entienda mal: es la mejor fuente de mejora.
"""
from __future__ import annotations

from alivia_edge.tuning.dataset import UNKNOWN_Q, Sample, dumps, gold
from alivia_edge.tuning.households import fixed_household


def cases() -> list[Sample]:
    h = fixed_household("la sala")
    h_kitchen = fixed_household("la cocina")
    d = h.catalog().devices
    door, l_sala, w_sala, l_coc, w_dor, cur, l_dor = (d[k] for k in (
        "door_10", "light_11", "window_12", "light_13", "window_14", "curtain_15", "light_16"))
    lights = [l_sala, l_coc, l_dor]
    out: list[Sample] = []

    def one(text: str, g: dict, tag: str, household=h) -> None:
        out.append(Sample(household, [{"role": "user", "content": text}], g, tag))

    one("Cierra la puerta", gold("ok", [(door, "cerrar")]), "hw_unica")
    one("prende la luz", gold("ok", [(l_sala, "encender")]), "hw_cuarto_del_usuario")
    one("Apaga la luz de la cocina, por favor.", gold("ok", [(l_coc, "apagar")]), "hw_con_cuarto")
    one("abre la ventana", gold("ok", [(w_sala, "abrir")]), "hw_cuarto_del_usuario")
    one("Abre la cortina.", gold("ok", [(cur, "abrir")]), "hw_unica")
    one("ya me voy a dormir", gold("ok", scene="todo"), "hw_escena")
    one("cierra todo", gold("ok", scene="todo"), "hw_escena")
    one("me voy a salir, deja todo cerrado", gold("ok", scene="todo"), "hw_escena")
    one("enciende todas las luces", gold("ok", [(x, "encender") for x in lights]), "hw_todas")
    one("oye apaga las luces", gold("ok", [(x, "apagar") for x in lights]), "hw_todas")
    one("cierra las ventanas", gold("ok", [(w_sala, "cerrar"), (w_dor, "cerrar")]), "hw_todas")
    one("apaga la luz del dormitorio y abre la cortina", gold("ok", [(l_dor, "apagar"), (cur, "abrir")]), "hw_multi")
    one("abre la puerta de la entrada y apaga la luz de la sala", gold("ok", [(door, "abrir"), (l_sala, "apagar")]), "hw_multi")
    one("Enciende la luz de la cocina y de la sala", gold("ok", [(l_coc, "encender"), (l_sala, "encender")]), "hw_multi")
    one("cierra la cortina y apaga la luz del dormitorio", gold("ok", [(cur, "cerrar"), (l_dor, "apagar")]), "hw_multi")
    one("Cierra la ventana del dormitorio", gold("ok", [(w_dor, "cerrar")]), "hw_con_cuarto")
    one("ábreme la puerta", gold("ok", [(door, "abrir")]), "hw_unica")
    one("¿puedes encender la luz del dormitorio?", gold("ok", [(l_dor, "encender")]), "hw_cortes")
    one("que se abra la ventana de la sala", gold("ok", [(w_sala, "abrir")]), "hw_con_cuarto")
    one("apaga la luz", gold("ok", [(l_sala, "apagar")]), "hw_cuarto_del_usuario")
    for text in ("¿Qué hora es?", "cuéntame un chiste", "gracias", "pon música", "enciende el ventilador"):
        one(text, gold("unknown", question=UNKNOWN_Q), "hw_fuera_de_tema")
    one("la luz de la cocina", gold("clarify", question="¿Qué desea hacer con la luz de la cocina?"), "hw_falta_accion")
    one("cierra la ventana", gold("clarify", question="¿Cuál desea cerrar: la de la sala o la del dormitorio?"), "hw_ambigua", h_kitchen)
    one("prende la luz", gold("ok", [(h_kitchen.catalog().devices["light_13"], "encender")]), "hw_cuarto_del_usuario", h_kitchen)

    g_prev = gold("ok", [(w_sala, "cerrar")])
    out.append(Sample(h, [{"role": "user", "content": "cierra la ventana de la sala"}, {"role": "assistant", "content": dumps(g_prev)},
                          {"role": "user", "content": "No. La del dormitorio"}], gold("ok", [(w_dor, "cerrar")]), "hw_correccion"))
    g_q = gold("clarify", question="¿Qué desea hacer con la luz de la cocina?")
    out.append(Sample(h, [{"role": "user", "content": "la luz de la cocina"}, {"role": "assistant", "content": dumps(g_q)},
                          {"role": "user", "content": "apágala"}], gold("ok", [(l_coc, "apagar")]), "hw_seguimiento"))
    return out
