import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main  # noqa: E402


class DebateControllerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.config = main.load_config()

    def state_with_questions(self, count, answered_count):
        state = main.initial_state(self.config["topic"])
        for number in range(1, count + 1):
            is_answered = number <= answered_count
            state["questions"].append({
                "number": number,
                "stage": main.stage_for(self.config, number)["name"],
                "asked_by": "A" if number % 2 else "B",
                "text": f"Sample question {number}?",
                "asker_context": "Sample context.",
                "asked_at": main.now_iso(),
                "answer": f"Sample answer {number}." if is_answered else None,
                "answered_by": "B" if number % 2 else "A" if is_answered else None,
                "answered_at": main.now_iso() if is_answered else None,
            })
        return state

    def test_stage_boundaries(self):
        self.assertEqual(main.stage_for(self.config, 1)["name"], "Foundations and definitions")
        self.assertEqual(main.stage_for(self.config, 41)["name"], "Cybersecurity transformation")
        self.assertEqual(main.stage_for(self.config, 200)["name"], "Convergence and final questions")

    def test_opening_when_empty(self):
        state = main.initial_state(self.config["topic"])
        self.assertEqual(main.choose_mode(state, 5, 10), "opening")

    def test_unanswered_question_generates_answer_and_next_question(self):
        state = self.state_with_questions(3, 2)
        self.assertEqual(main.choose_mode(state, 5, 10), "answer_and_ask")

    def test_target_question_is_answered_without_asking_another(self):
        state = self.state_with_questions(5, 4)
        self.assertEqual(main.choose_mode(state, 5, 10), "answer_only")

    def test_checkpoint_question_is_answered_without_asking_another(self):
        state = self.state_with_questions(10, 9)
        self.assertEqual(main.choose_mode(state, 20, 10), "answer_only")

    def test_resume_after_answered_target_asks_next_question(self):
        state = self.state_with_questions(5, 5)
        self.assertEqual(main.choose_mode(state, 20, 10), "continue_and_ask")
        self.assertIsNone(main.choose_mode(state, 5, 10))

    def test_output_validation_rejects_missing_question(self):
        output = {
            "response": "A sufficiently long answer to satisfy the schema requirements.",
            "next_question": None,
            "key_insights": [],
            "disagreements": [],
            "assumptions": [],
            "predictions_to_test": [],
            "confidence": 0.5,
        }
        with self.assertRaises(RuntimeError):
            main.validate_output(output, "opening")

    def test_output_validation_accepts_answer_only(self):
        output = {
            "response": "A sufficiently long answer to satisfy the schema requirements.",
            "next_question": None,
            "key_insights": ["A short insight"],
            "disagreements": [],
            "assumptions": [],
            "predictions_to_test": [],
            "confidence": 0.7,
        }
        main.validate_output(output, "answer_only")


if __name__ == "__main__":
    unittest.main(verbosity=2)
