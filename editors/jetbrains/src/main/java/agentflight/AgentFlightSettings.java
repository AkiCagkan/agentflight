package agentflight;

import com.intellij.ide.util.PropertiesComponent;
import com.intellij.openapi.options.Configurable;
import com.intellij.ui.components.JBLabel;
import com.intellij.ui.components.JBTextField;
import com.intellij.util.ui.FormBuilder;
import org.jetbrains.annotations.Nls;
import org.jetbrains.annotations.Nullable;

import javax.swing.JComponent;

/** Settings | Tools | AgentFlight: the Python interpreter that has the agentflight package. */
public class AgentFlightSettings implements Configurable {
    private JBTextField python;

    @Override
    public @Nls String getDisplayName() {
        return "AgentFlight";
    }

    @Override
    public @Nullable JComponent createComponent() {
        python = new JBTextField();
        return FormBuilder.createFormBuilder()
                .addLabeledComponent(new JBLabel("Python interpreter with the agentflight package:"), python, 1, false)
                .addComponentToRightColumn(new JBLabel("<html><small>Empty: <code>python</code> on PATH (or the AGENTFLIGHT_PYTHON environment variable).</small></html>"))
                .addComponentFillVertically(new javax.swing.JPanel(), 0)
                .getPanel();
    }

    @Override
    public boolean isModified() {
        return python != null && !python.getText().trim().equals(PropertiesComponent.getInstance().getValue(PanelLauncher.PYTHON_SETTING, ""));
    }

    @Override
    public void apply() {
        PropertiesComponent.getInstance().setValue(PanelLauncher.PYTHON_SETTING, python.getText().trim(), "");
    }

    @Override
    public void reset() {
        if (python != null) python.setText(PropertiesComponent.getInstance().getValue(PanelLauncher.PYTHON_SETTING, ""));
    }
}
