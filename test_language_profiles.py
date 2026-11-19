"""Language profiles learned from tagged subtitles: measured before use, refuse what they don't know."""
import random
import sqlite3
import unittest

import language_profiles as lp

VOCAB = {
    "en": "the you and what that it's don't i'm this with have your just know right there going think about "
          "would could should never people really something nothing because where when here they them".split(),
    "nl": "het een niet wat dat ik je van maar zijn hij wel jij ook nog weet goed gaan moet kunnen hebben "
          "waarom hier daar mensen niets iets altijd zeggen alleen eigenlijk gewoon misschien zou".split(),
    "es": "que eso pero con una del usted muy aquí tengo bueno señor nada todo cuando donde porque siempre "
          "nunca hacer puedo quiero esto estás gracias vamos ahora tiempo gente".split(),
    "af": "ek jy nie dit is wat het die en sy hy ons hulle weet gaan moet kan baie goed hier daar mense niks "
          "iets altyd sê net eintlik gewoon miskien sal".split(),
}


def track(language, seed):
    rng = random.Random(f"{language}{seed}")
    lines = [" ".join(rng.choice(VOCAB[language]) for _ in range(rng.randint(4, 9))).capitalize() + "."
             for _ in range(200)]
    return "\n".join(f"{i}\n00:00:{i % 60:02d},000 --> 00:00:{i % 60:02d},900\n{l}\n" for i, l in enumerate(lines, 1))


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.addCleanup(self.conn.close)

    def learn(self, language, n):
        for seed in range(n):
            lp.learn(self.conn, language, track(language, seed), now=seed)

    def test_too_little_data_is_not_used(self):
        self.learn("en", 3)
        model = lp.Model(self.conn)
        self.assertFalse(model.report["en"]["used"])
        self.assertIsNone(model.detect(track("en", 99)))

    def test_learned_languages_are_measured_and_used(self):
        for language in ("en", "nl", "es"):
            self.learn(language, 20)
        model = lp.Model(self.conn)
        for language in ("en", "nl", "es"):
            self.assertTrue(model.report[language]["used"], model.report)
            self.assertGreaterEqual(model.report[language]["tests"], lp.MIN_HOLDOUT)
            self.assertEqual(model.detect(track(language, 999)), language)

    def test_an_unlearned_neighbour_is_refused_not_mislabelled(self):
        for language in ("en", "nl", "es"):
            self.learn(language, 20)
        model = lp.Model(self.conn)
        self.assertIsNone(model.detect(track("af", 1)))  # Afrikaans, never learned: not taken for Dutch

    def test_duplicates_and_short_texts_are_ignored(self):
        self.assertIn(lp.learn(self.conn, "en", track("en", 1)), ("learned", "test"))
        self.assertEqual(lp.learn(self.conn, "en", track("en", 1)), "known")
        self.assertEqual(lp.learn(self.conn, "en", "1\n00:00:01,000 --> 00:00:02,000\nHi.\n"), "too short")


if __name__ == "__main__":
    unittest.main()
