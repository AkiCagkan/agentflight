package agentflight;

import com.intellij.ide.util.PropertiesComponent;

import java.io.IOException;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.Paths;
import java.util.ArrayList;
import java.util.List;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

/** Finds or starts the AgentFlight panel server (`python -m agentflight watch`). */
public final class PanelLauncher {
    public static final String PYTHON_SETTING = "agentflight.pythonPath";
    private static Process process;

    private PanelLauncher() {
    }

    public static String python() {
        String setting = PropertiesComponent.getInstance().getValue(PYTHON_SETTING, "").trim();
        if (!setting.isEmpty()) return setting;
        String env = System.getenv("AGENTFLIGHT_PYTHON");
        if (env != null && !env.isBlank()) return env;
        return System.getProperty("os.name", "").toLowerCase().contains("win") ? "python" : "python3";
    }

    static Path settingsDir() {
        String env = System.getenv("AGENTFLIGHT_HOME");
        return env != null && !env.isBlank() ? Paths.get(env) : Paths.get(System.getProperty("user.home"), ".agentflight");
    }

    /** Port of the running panel's web server, or -1. */
    public static int runningPanelPort() {
        Path file = settingsDir().resolve("panel.json");
        if (!Files.isRegularFile(file)) return -1;
        try {
            String json = Files.readString(file, StandardCharsets.UTF_8);
            int port = number(json, "port");
            int runnerPort = number(json, "runner_port");
            if (port > 0 && runnerPort > 0 && isOpen(port) && isOpen(runnerPort)) return port;
        } catch (IOException ignored) {
        }
        return -1;
    }

    private static int number(String json, String key) {
        Matcher m = Pattern.compile("\"" + key + "\"\\s*:\\s*(\\d+)").matcher(json);
        return m.find() ? Integer.parseInt(m.group(1)) : -1;
    }

    private static boolean isOpen(int port) {
        try (Socket s = new Socket()) {
            s.connect(new InetSocketAddress("127.0.0.1", port), 400);
            return true;
        } catch (IOException e) {
            return false;
        }
    }

    /** Returns the panel URL, starting the panel server if needed (blocking; call off the UI thread). */
    public static synchronized String ensurePanel() throws IOException {
        int port = runningPanelPort();
        if (port > 0) return "http://127.0.0.1:" + port + "/";
        List<String> command = new ArrayList<>(List.of(python(), "-m", "agentflight", "watch", "--no-browser", "--port", "7790"));
        ProcessBuilder pb = new ProcessBuilder(command).redirectErrorStream(true).redirectOutput(ProcessBuilder.Redirect.DISCARD);
        pb.environment().put("PYTHONIOENCODING", "utf-8");
        process = pb.start();
        for (int i = 0; i < 100; i++) {
            try {
                Thread.sleep(150);
            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                break;
            }
            port = runningPanelPort();
            if (port > 0) return "http://127.0.0.1:" + port + "/";
            if (!process.isAlive() && process.exitValue() != 0) break;
        }
        throw new IOException("Could not start the AgentFlight panel with \"" + python() + " -m agentflight watch\". "
                + "Install the agentflight package (pip install agentflight) or set the Python path in Settings | Tools | AgentFlight.");
    }

    public static void stop() {
        if (process != null && process.isAlive()) process.destroy();
    }
}
