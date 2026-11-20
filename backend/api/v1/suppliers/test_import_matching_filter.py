from django.test import SimpleTestCase

from api.v1.suppliers.product_import_matching import distinguishing_words, shares_distinguishing_word


class DistinguishingWordsTests(SimpleTestCase):
    def test_head_word_brand_numbers_and_packaging_are_not_distinguishing(self):
        words = distinguishing_words('Соус Барбекю СП ТМ "Yakimal", 1/6, бутылка 350 г')
        self.assertEqual(words, {"барбе"})

    def test_names_without_a_distinguishing_word_have_none(self):
        self.assertEqual(distinguishing_words("Coca-Cola 0,5"), set())
        self.assertEqual(distinguishing_words("Соус"), set())


class SharesDistinguishingWordTests(SimpleTestCase):
    raw = 'Соус Барбекю СП ТМ "Yakimal", 1/6'

    def test_same_variety_in_other_pack_is_a_candidate(self):
        self.assertTrue(shares_distinguishing_word(self.raw, 'Соус томатный "Барбекю" 1кг в коробке (6/1)'))
        self.assertTrue(shares_distinguishing_word(self.raw, 'Соус томатный "Барбекю" 350гр бутылка (12/1)'))

    def test_other_variety_of_the_same_brand_is_not(self):
        self.assertFalse(shares_distinguishing_word(self.raw, "Соус Унаги Yakimal 1.8л"))
        self.assertFalse(shares_distinguishing_word(self.raw, "Соус рыбный 1л/12шт Yakimal"))

    def test_buckwheat_is_not_barley(self):
        self.assertFalse(
            shares_distinguishing_word("Крупа гречневая ТМ Мистраль 900г", "Крупа ячневая ТМ Мистраль 900г")
        )
        self.assertTrue(shares_distinguishing_word("Крупа гречневая ТМ Мистраль 900г", "Крупа гречневая ядрица 800 г"))

    def test_inflection_and_abbreviation_still_match(self):
        self.assertTrue(shares_distinguishing_word("Рис круглозерный 1 кг", "Рис круглозёрный шлифованный"))
        self.assertTrue(shares_distinguishing_word("Мука пшеничная хлебопек. в/с 25кг", "Мука пшеничная в/с 50 кг"))

    def test_without_a_distinguishing_word_the_similarity_decides(self):
        self.assertTrue(shares_distinguishing_word("Coca-Cola 0,5", "Coca-Cola 1 л"))
        self.assertTrue(shares_distinguishing_word("Соус", "Соус томатный"))
