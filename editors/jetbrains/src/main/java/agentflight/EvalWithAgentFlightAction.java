package agentflight;

/** Runs the evals in the current file with `python -m agentflight eval`. */
public class EvalWithAgentFlightAction extends RunWithAgentFlightAction {
    @Override
    protected String command() {
        return "eval";
    }

    @Override
    protected String title() {
        return "AgentFlight evals";
    }
}
