package agentflight;

import com.intellij.openapi.application.ApplicationManager;
import com.intellij.openapi.fileEditor.OpenFileDescriptor;
import com.intellij.openapi.project.DumbAware;
import com.intellij.openapi.project.Project;
import com.intellij.openapi.vfs.LocalFileSystem;
import com.intellij.openapi.vfs.VirtualFile;
import com.intellij.openapi.wm.ToolWindow;
import com.intellij.openapi.wm.ToolWindowFactory;
import com.intellij.ui.components.JBLabel;
import com.intellij.ui.content.Content;
import com.intellij.ui.content.ContentFactory;
import com.intellij.ui.jcef.JBCefApp;
import com.intellij.ui.jcef.JBCefBrowser;
import com.intellij.ui.jcef.JBCefBrowserBase;
import com.intellij.ui.jcef.JBCefJSQuery;
import org.cef.browser.CefBrowser;
import org.cef.browser.CefFrame;
import org.cef.handler.CefLoadHandlerAdapter;
import org.jetbrains.annotations.NotNull;

import javax.swing.JPanel;
import javax.swing.SwingConstants;
import java.awt.BorderLayout;

/** The "AgentFlight" tool window: the live agent panel in a JCEF browser. */
public final class AgentFlightToolWindowFactory implements ToolWindowFactory, DumbAware {

    @Override
    public void createToolWindowContent(@NotNull Project project, @NotNull ToolWindow toolWindow) {
        JPanel container = new JPanel(new BorderLayout());
        container.add(new JBLabel("Starting the AgentFlight panel…", SwingConstants.CENTER), BorderLayout.CENTER);
        Content content = ContentFactory.getInstance().createContent(container, "", false);
        toolWindow.getContentManager().addContent(content);

        if (!JBCefApp.isSupported()) {
            container.removeAll();
            container.add(new JBLabel("This IDE has no embedded browser (JCEF). Run \"agentflight watch\" and open the panel in a browser.",
                    SwingConstants.CENTER), BorderLayout.CENTER);
            return;
        }
        ApplicationManager.getApplication().executeOnPooledThread(() -> {
            String panelUrl;
            try {
                panelUrl = PanelLauncher.ensurePanel();
            } catch (Exception e) {
                String message = e.getMessage();
                ApplicationManager.getApplication().invokeLater(() -> {
                    container.removeAll();
                    container.add(new JBLabel("<html><div style='padding:12px'>" + message + "</div></html>", SwingConstants.CENTER),
                            BorderLayout.CENTER);
                    container.revalidate();
                });
                return;
            }
            String url = panelUrl;
            ApplicationManager.getApplication().invokeLater(() -> {
                JBCefBrowser browser = new JBCefBrowser(url);
                JBCefJSQuery query = JBCefJSQuery.create((JBCefBrowserBase) browser);
                // The panel calls window.agentFlightEditor("<line>|<absolute path>") for "Line N" / "Open instructions".
                query.addHandler(data -> {
                    int separator = data.indexOf('|');
                    if (separator > 0) {
                        int line;
                        try {
                            line = Integer.parseInt(data.substring(0, separator));
                        } catch (NumberFormatException e) {
                            line = 1;
                        }
                        String path = data.substring(separator + 1);
                        int target = line;
                        ApplicationManager.getApplication().invokeLater(() -> openFile(project, path, target));
                    }
                    return null;
                });
                browser.getJBCefClient().addLoadHandler(new CefLoadHandlerAdapter() {
                    @Override
                    public void onLoadEnd(CefBrowser cefBrowser, CefFrame frame, int httpStatusCode) {
                        if (frame.isMain()) {
                            cefBrowser.executeJavaScript("window.agentFlightEditor = function (m) {" + query.inject("m") + "};",
                                    cefBrowser.getURL(), 0);
                        }
                    }
                }, browser.getCefBrowser());
                container.removeAll();
                container.add(browser.getComponent(), BorderLayout.CENTER);
                container.revalidate();
                container.repaint();
            });
        });
    }

    static void openFile(Project project, String path, int line) {
        VirtualFile file = LocalFileSystem.getInstance().refreshAndFindFileByPath(path.replace('\\', '/'));
        if (file != null) new OpenFileDescriptor(project, file, Math.max(0, line - 1), 0).navigate(true);
    }
}
