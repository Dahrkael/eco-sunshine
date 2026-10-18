"""Regression tests for the web panel i18n (browser-language based).

Guards: both dictionaries define the exact same key set, every data-i18n
reference in the HTML resolves, and no Spanish UI string remains hardcoded
outside the es dictionary (dynamic strings must go through t()).
"""

from __future__ import annotations

import re

from ecovacs_bridge.webpanel import INDEX_HTML


def extract_i18n_dicts() -> tuple[set[str], set[str]]:
    m = re.search(r"const I18N = \{\s*es: \{(.*?)\},\s*en: \{(.*?)\}\s*\};",
                  INDEX_HTML, re.S)
    assert m, "I18N dictionaries not found in INDEX_HTML"
    keys = []
    for body in m.groups():
        # keys are always followed by a quoted value (distinguishes them
        # from "word:" fragments inside translated strings)
        ks = set(re.findall(r"(?:^|,)\s*(\w+):\s*'", body, re.M))
        keys.append(ks)
    return keys[0], keys[1]


class TestI18n:
    def test_ambos_idiomas_mismas_claves(self):
        es, en = extract_i18n_dicts()
        assert es, "empty es dictionary"
        assert es == en, f"key mismatch: es-only={es - en} en-only={en - es}"

    def test_toda_referencia_data_i18n_resuelve(self):
        es, en = extract_i18n_dicts()
        refs = set(re.findall(r'data-i18n="(\w+)"', INDEX_HTML))
        missing = refs - en
        assert not missing, f"data-i18n keys without translation: {missing}"

    def test_claves_usadas_en_t_resuelven(self):
        es, en = extract_i18n_dicts()
        # word-boundary lookbehind: getContext('2d') no es un t()
        refs = set(re.findall(r"(?<![A-Za-z])t\('(\w+)'\)", INDEX_HTML))
        missing = refs - en
        assert not missing, f"t() keys without translation: {missing}"

    def test_deteccion_de_idioma_por_navegador(self):
        """navigator.languages → es if any starts with 'es', else en."""
        assert "navigator.languages" in INDEX_HTML
        assert "startsWith('es')" in INDEX_HTML
        # the default textContent of static nodes is replaced on load
        assert "document.querySelectorAll('[data-i18n]')" in INDEX_HTML

    def test_sin_espanol_hardcodeado_fuera_del_diccionario(self):
        """No Spanish UI fragments in the HTML body outside the es dict."""
        body = INDEX_HTML
        # strip the es dictionary block before scanning
        m = re.search(r"const I18N = \{.*?\};", body, re.S)
        assert m
        body = body.replace(m.group(0), "")
        for frag in ("Configuración", "Habitaciones", "Estadísticas", "Consumibles",
                     "Borrar", "Recuperar", "Iniciar", "Pausa", "Localizar",
                     "Conectando al robot"):
            assert frag not in body, f"hardcoded Spanish string survived: {frag!r}"
