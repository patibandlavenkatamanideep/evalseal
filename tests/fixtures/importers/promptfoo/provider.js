// A deterministic, offline promptfoo provider.
//
// No network, no key, no model. It answers from a fixed table keyed by the question
// text, so the same config produces the same export on any machine. One question is
// answered wrongly on purpose and one raises an error, because an importer that has
// only seen passing rows has not been tested.
const ANSWERS = {
  'What is 2 + 2?': '4',
  'What is 10 divided by 4?': '2.5',
  'How many sides does a hexagon have?': '7',          // deliberately wrong (6)
  'What is the square root of 144?': '12',
};

class DeterministicProvider {
  id() {
    return 'deterministic-local:fixture-v1';
  }

  async callApi(prompt, context) {
    const question = (context && context.vars && context.vars.question) || '';
    if (question.startsWith('UNANSWERABLE')) {
      // A provider-level failure, which promptfoo records as an error rather than a
      // failed assertion. The two are different things and an importer must keep them
      // apart.
      return { error: 'deterministic provider: no answer configured for this question' };
    }
    const output = ANSWERS[question];
    if (output === undefined) {
      return { error: `deterministic provider: unknown question ${JSON.stringify(question)}` };
    }
    return {
      output,
      tokenUsage: { total: 0, prompt: 0, completion: 0 },
    };
  }
}

module.exports = DeterministicProvider;
