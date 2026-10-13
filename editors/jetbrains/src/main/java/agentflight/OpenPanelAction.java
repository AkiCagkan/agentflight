package agentflight;

import com.intellij.openapi.actionSystem.ActionUpdateThread;
import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.intellij.openapi.wm.ToolWindow;
import com.intellij.openapi.wm.ToolWindowManager;
import org.jetbrains.annotations.NotNull;

/** Shows the AgentFlight tool window. */
public class OpenPanelAction extends AnAction {
    @Override
    public @NotNull ActionUpdateThread getActionUpdateThread() {
        return ActionUpdateThread.BGT;
    }

    @Override
    public void update(@NotNull AnActionEvent e) {
        e.getPresentation().setEnabled(e.getProject() != null);
    }

    @Override
    public void actionPerformed(@NotNull AnActionEvent e) {
        if (e.getProject() == null) return;
        ToolWindow window = ToolWindowManager.getInstance(e.getProject()).getToolWindow("AgentFlight");
        if (window != null) window.show();
    }
}
