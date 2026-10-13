package agentflight;

import com.intellij.execution.ExecutionException;
import com.intellij.execution.RunContentExecutor;
import com.intellij.execution.configurations.GeneralCommandLine;
import com.intellij.execution.process.OSProcessHandler;
import com.intellij.notification.NotificationGroupManager;
import com.intellij.notification.NotificationType;
import com.intellij.openapi.actionSystem.ActionUpdateThread;
import com.intellij.openapi.actionSystem.AnAction;
import com.intellij.openapi.actionSystem.AnActionEvent;
import com.intellij.openapi.actionSystem.CommonDataKeys;
import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.fileEditor.FileDocumentManager;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.vfs.VirtualFile;
import com.intellij.openapi.wm.ToolWindowManager;
import org.jetbrains.annotations.NotNull;

import java.nio.charset.StandardCharsets;

/** Runs the current file with `python -m agentflight run`; its agents show up in the AgentFlight tool window. */
public class RunWithAgentFlightAction extends AnAction {

    protected String command() {
        return "run";
    }

    protected String title() {
        return "AgentFlight";
    }

    @Override
    public @NotNull ActionUpdateThread getActionUpdateThread() {
        return ActionUpdateThread.BGT;
    }

    @Override
    public void update(@NotNull AnActionEvent e) {
        VirtualFile file = e.getData(CommonDataKeys.VIRTUAL_FILE);
        String extension = file == null ? null : file.getExtension();
        e.getPresentation().setEnabledAndVisible(e.getProject() != null && "py".equals(extension));
    }

    @Override
    public void actionPerformed(@NotNull AnActionEvent e) {
        Project project = e.getProject();
        VirtualFile file = e.getData(CommonDataKeys.VIRTUAL_FILE);
        if (project == null || file == null) return;
        FileDocumentManager.getInstance().saveAllDocuments();
        var window = ToolWindowManager.getInstance(project).getToolWindow("AgentFlight");
        if (window != null) window.show();
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            try {
                PanelLauncher.ensurePanel();
            } catch (Exception error) {
                notifyError(project, error.getMessage());
                return;
            }
            ApplicationManager.getApplication().invokeLater(() -> runFile(project, file));
        });
    }

    private void runFile(Project project, VirtualFile file) {
        GeneralCommandLine commandLine = new GeneralCommandLine(PanelLauncher.python(), "-m", "agentflight", command(), file.getPath())
                .withWorkDirectory(file.getParent().getPath())
                .withCharset(StandardCharsets.UTF_8)
                .withEnvironment("PYTHONIOENCODING", "utf-8")
                .withEnvironment("PYTHONUNBUFFERED", "1");
        try {
            OSProcessHandler handler = new OSProcessHandler(commandLine);
            new RunContentExecutor(project, handler).withTitle(title() + ": " + file.getName()).withActivateToolWindow(true).run();
        } catch (ExecutionException error) {
            notifyError(project, error.getMessage());
        }
    }

    static void notifyError(Project project, String message) {
        NotificationGroupManager.getInstance().getNotificationGroup("AgentFlight")
                .createNotification(message == null ? "AgentFlight error" : message, NotificationType.ERROR).notify(project);
    }
}
