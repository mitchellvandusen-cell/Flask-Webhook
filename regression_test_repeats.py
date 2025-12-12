#!/usr/bin/env python3
"""
Regression test suite for repeat-intent protection.
Run before every deploy: python regression_test_repeats.py

Tests:
1. Theme-based blocking (keyword matching)
2. spaCy vector similarity blocking (0.85 threshold)
3. False positive protection (valid questions not blocked)
4. Sloppy text / misspellings handling
"""

import unittest
import sys
import os

QUESTION_THEMES = {
    "retirement_portability": [
        "continue after retirement", "leave your job", "retire", "portable", 
        "convert it", "goes with you", "when you leave", "portability",
        "if you quit", "stop working", "leaving the company"
    ],
    "policy_type": [
        "term or whole", "term or permanent", "what type", "kind of policy",
        "is it term", "is it whole life", "iul", "universal life"
    ],
    "living_benefits": [
        "living benefits", "accelerated death", "chronic illness", 
        "critical illness", "terminal illness", "access while alive"
    ],
    "coverage_goal": [
        "what made you", "why did you", "what's the goal", "what were you",
        "originally looking", "why coverage", "what prompted", "got you looking",
        "what got you"
    ],
    "other_policies": [
        "other policies", "any other", "additional coverage", "also have",
        "multiple policies", "work policy", "another plan"
    ],
    "motivation": [
        "what's on your mind", "what's been on", "what specifically", 
        "what are you thinking", "what concerns you"
    ]
}

def get_question_theme(text):
    """Return the theme(s) of a message."""
    text_lower = text.lower()
    themes = []
    for theme, keywords in QUESTION_THEMES.items():
        if any(kw in text_lower for kw in keywords):
            themes.append(theme)
    return themes

def check_theme_overlap(reply, recent_messages):
    """Check if reply shares themes with recent messages."""
    reply_themes = get_question_theme(reply)
    if not reply_themes:
        return False, None
    
    for prev_msg in recent_messages[-5:]:
        prev_themes = get_question_theme(prev_msg)
        shared = set(reply_themes) & set(prev_themes)
        if shared:
            return True, list(shared)[0]
    return False, None


class TestThemeBlocking(unittest.TestCase):
    """Test keyword-based theme detection and blocking."""
    
    def test_retirement_theme_detected(self):
        """Retirement questions should be caught."""
        variants = [
            "What happens when you retire?",
            "Is this portable if you leave your job?",
            "Can you convert it later?",
            "Does it go with you when you leave?"
        ]
        for q in variants:
            themes = get_question_theme(q)
            self.assertIn("retirement_portability", themes, f"Failed to detect theme: {q}")
    
    def test_motivation_theme_detected(self):
        """Motivation questions should be caught."""
        variants = [
            "What got you looking into coverage?",
            "What's on your mind about insurance?",
            "What prompted you to look at this?",
            "Why did you start looking?"
        ]
        for q in variants:
            themes = get_question_theme(q)
            has_theme = "motivation" in themes or "coverage_goal" in themes
            self.assertTrue(has_theme, f"Failed to detect motivation/goal theme: {q}")
    
    def test_theme_blocking_works(self):
        """Same theme in history should block new question."""
        history = ["What happens when you retire from your job?"]
        
        blocked_variants = [
            "Is this portable if you leave the company?",
            "What about if you stop working?",
            "Can you convert it when you retire?"
        ]
        
        for variant in blocked_variants:
            is_blocked, theme = check_theme_overlap(variant, history)
            self.assertTrue(is_blocked, f"Should have blocked: {variant}")
    
    def test_different_themes_not_blocked(self):
        """Different themes should NOT be blocked."""
        history = ["What happens when you retire?"]  # retirement_portability
        
        safe_questions = [
            "How much coverage are you looking for?",
            "Do you have any health conditions?",
            "Is it term or whole life?"  # policy_type - different theme
        ]
        
        for q in safe_questions:
            is_blocked, _ = check_theme_overlap(q, history)
            self.assertFalse(is_blocked, f"Incorrectly blocked: {q}")


class TestVectorSimilarity(unittest.TestCase):
    """Test spaCy vector similarity blocking."""
    
    @classmethod
    def setUpClass(cls):
        """Load spaCy model once for all tests."""
        try:
            from nlp_memory import get_nlp
            cls.nlp = get_nlp()
            cls.has_vectors = cls.nlp.vocab.vectors.shape[0] > 0
        except Exception as e:
            print(f"Warning: Could not load spaCy: {e}")
            cls.nlp = None
            cls.has_vectors = False
    
    def test_similar_questions_high_similarity(self):
        """Semantically similar questions should have high similarity."""
        if not self.nlp or not self.has_vectors:
            self.skipTest("spaCy model not available or no vectors")
        
        pairs = [
            ("What got you looking into insurance?", "What made you want to get coverage?"),
            ("Do you have any health conditions?", "Any medical issues I should know about?"),
            ("How much coverage do you need?", "What amount of coverage are you thinking?")
        ]
        
        for q1, q2 in pairs:
            doc1 = self.nlp(q1)
            doc2 = self.nlp(q2)
            sim = doc1.similarity(doc2)
            self.assertGreater(sim, 0.80, f"Expected high similarity:\n  '{q1}'\n  '{q2}'\n  Got: {sim:.3f}")
    
    def test_unrelated_questions_lower_similarity(self):
        """Unrelated questions should have lower similarity than paraphrases."""
        if not self.nlp or not self.has_vectors:
            self.skipTest("spaCy model not available or no vectors")
        
        pairs = [
            ("What got you looking into insurance?", "I love pizza for dinner"),
            ("Do you have health conditions?", "The cat sat on the mat"),
        ]
        
        for q1, q2 in pairs:
            doc1 = self.nlp(q1)
            doc2 = self.nlp(q2)
            sim = doc1.similarity(doc2)
            self.assertLess(sim, 0.80, f"Expected lower similarity:\n  '{q1}'\n  '{q2}'\n  Got: {sim:.3f}")
    
    def test_blocking_threshold(self):
        """Test the 0.85 blocking threshold catches paraphrases."""
        if not self.nlp or not self.has_vectors:
            self.skipTest("spaCy model not available or no vectors")
        
        base = "What got you looking at life insurance a while back?"
        paraphrases = [
            "What made you want to look into coverage?",
            "Why were you shopping for insurance before?",
        ]
        
        for para in paraphrases:
            doc1 = self.nlp(base)
            doc2 = self.nlp(para)
            sim = doc1.similarity(doc2)
            self.assertGreater(sim, 0.85, 
                f"Paraphrase should be caught (>0.85):\n  Original: '{base}'\n  Paraphrase: '{para}'\n  Similarity: {sim:.3f}")


class TestFalsePositives(unittest.TestCase):
    """Ensure valid new questions are NOT incorrectly blocked."""
    
    def test_progression_questions_not_blocked(self):
        """Natural progression questions should pass."""
        history = ["What got you looking into coverage?"]
        
        progression = [
            "Got it. How much coverage are you thinking?",
            "Makes sense. Do you have any health conditions?",
            "I understand. When would you want to get started?",
            "Perfect. I have some time tonight or tomorrow morning."
        ]
        
        for q in progression:
            is_blocked, _ = check_theme_overlap(q, history)
            self.assertFalse(is_blocked, f"Progression blocked incorrectly: {q}")
    
    def test_appointment_offers_not_blocked(self):
        """Appointment offers should never be blocked."""
        history = [
            "What got you looking?",
            "Is it term or whole life?",
            "Do you have living benefits?"
        ]
        
        appointments = [
            "I have some time at 6:30 tonight or 10:15 tomorrow.",
            "Want to hop on a quick call to go over options?",
            "I can do a 15 minute review, which works better?"
        ]
        
        for q in appointments:
            is_blocked, _ = check_theme_overlap(q, history)
            self.assertFalse(is_blocked, f"Appointment blocked incorrectly: {q}")


class TestSloppyText(unittest.TestCase):
    """Test handling of informal/messy text."""
    
    def test_lowercase_detection(self):
        """Theme detection should be case-insensitive."""
        variants = [
            "WHAT MADE YOU LOOK INTO INSURANCE?",
            "what made you look into insurance?",
            "What Made You Look Into Insurance?"
        ]
        
        for q in variants:
            themes = get_question_theme(q)
            self.assertTrue(len(themes) > 0, f"Failed on case variant: {q}")
    
    def test_partial_matches(self):
        """Keywords should match as substrings."""
        test_cases = [
            ("Is this thing portable?", "retirement_portability"),
            ("What about living benefits on this?", "living_benefits"),
        ]
        
        for text, expected_theme in test_cases:
            themes = get_question_theme(text)
            self.assertIn(expected_theme, themes, f"Failed partial match: {text}")


def run_tests():
    """Run all tests and return success status."""
    print("=" * 60)
    print("REPEAT-INTENT REGRESSION TEST SUITE")
    print("=" * 60)
    print()
    
    loader = unittest.TestLoader()
    suite = unittest.TestSuite()
    
    suite.addTests(loader.loadTestsFromTestCase(TestThemeBlocking))
    suite.addTests(loader.loadTestsFromTestCase(TestVectorSimilarity))
    suite.addTests(loader.loadTestsFromTestCase(TestFalsePositives))
    suite.addTests(loader.loadTestsFromTestCase(TestSloppyText))
    
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    
    print()
    print("=" * 60)
    if result.wasSuccessful():
        print("ALL TESTS PASSED - Safe to deploy!")
        print("=" * 60)
        return True
    else:
        print("TESTS FAILED - DO NOT DEPLOY")
        print(f"  Failures: {len(result.failures)}")
        print(f"  Errors: {len(result.errors)}")
        print("=" * 60)
        return False


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
