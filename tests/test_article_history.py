"""Regression checks for cross-column article history, not model writing quality."""
import copy
import unittest
from test_registry import case, fixture, registry


class ArticleHistory(unittest.TestCase):
    def test_article_only_arguments_are_compared(self):
        history = fixture()
        old = history['cases'][0]
        for field in ('question', 'takeaway'):
            old.pop(field)
        old['analyses'] = {'earlier': {'question': '机器标签如何定义一个人', 'takeaway': '分类规则把社会评价藏进技术流程'}}
        candidate = case('another', title='Another work', artist='Another artist')
        candidate.pop('question')
        candidate.pop('takeaway')
        candidate['analyses'] = {'new': copy.deepcopy(old['analyses']['earlier'])}
        result = registry.assess_candidate(history, candidate)
        overlaps = [r for r in result['risks'] if r['category'] == 'argument_overlap_warning']
        self.assertEqual({r['analysis_id'] for r in overlaps}, {'earlier'})
        self.assertEqual({r['candidate_analysis_id'] for r in overlaps}, {'new'})
        self.assertEqual(result['argument_coverage']['historical_records_incomplete'], 0)

    def test_cross_column_history_is_not_filtered_out(self):
        history = fixture()
        history['cases'][0]['analyses'] = {'first-skill': {'skill_name': 'tech-in-art', 'question': '材料如何让观众看见数字图像？', 'takeaway': '木块将数字图像变成物质表面'}}
        result = registry.assess_candidate(history, case('new', artist='Different', title='Different'))
        self.assertTrue(any(r.get('analysis_id') == 'first-skill' for r in result['risks']))

    def test_missing_old_questions_are_reported_without_novelty_certification(self):
        history = fixture()
        history['cases'][0]['question'] = None
        history['cases'][0]['takeaway'] = None
        result = registry.assess_candidate(history, case('new', artist='Different', title='Different'))
        self.assertEqual(result['argument_coverage']['historical_records_incomplete'], 1)
        missing = [r for r in result['risks'] if r['category'] == 'argument_history_incomplete']
        self.assertEqual(missing[0]['missing_fields'], ['question', 'takeaway'])
        self.assertTrue(result['requires_human_review'])

    def test_invalid_article_objects_are_validation_errors(self):
        for malformed in ([], {'a': 'text'}, {'a': {'question': ['bad']}}, {'': {}}):
            with self.subTest(malformed=malformed):
                history = fixture()
                history['cases'][0]['analyses'] = malformed
                self.assertTrue(registry.validate_registry(history)['errors'])

    def test_sequential_article_updates_preserve_prior_column(self):
        history = fixture()
        history['cases'][0]['analyses'] = {'first': {'question': 'old', 'takeaway': 'old conclusion'}}
        merged, _ = registry.merge_registry(history, {'cases': [{'id': 'wooden', 'analyses': {'second': {'question': 'new', 'takeaway': None}}}]}, 'update')
        self.assertEqual(merged['cases'][0]['analyses']['first'], history['cases'][0]['analyses']['first'])
        self.assertIn('second', merged['cases'][0]['analyses'])

    def test_unrelated_keywords_still_require_human_review(self):
        result = registry.assess_candidate(fixture(), case('new', artist='Different', title='Different', question='xyz', takeaway='abc'))
        self.assertFalse(any(r['category'] == 'argument_overlap_warning' for r in result['risks']))
        self.assertTrue(result['requires_human_review'])


if __name__ == '__main__':
    unittest.main()
