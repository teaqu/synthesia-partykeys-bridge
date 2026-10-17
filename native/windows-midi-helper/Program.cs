// Microsoft Windows MIDI Services Preview 11. Windows 11 25H2+.
using System.Text.Json;
using System.Security.Cryptography;
using System.Text;
using Windows.Devices.Midi2;
using Windows.Devices.Midi2.Transports.Loopback;
using Windows.Devices.Midi2.Utilities.Messages;

try
{
    string name = args.Length == 2 && args[0] == "--name" ? args[1] : "Synthesia Lights";
    if (args.Length != 0 && !(args.Length == 2 && args[0] == "--name"))
        throw new ArgumentException("Usage: PartyKeys.Midi [--name NAME]");
    if (string.IsNullOrWhiteSpace(name) || name.Length > 24)
        throw new ArgumentException("MIDI port name must be between 1 and 24 characters.");
    string mutexName = "Local\\PartyKeysMidi-" + Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(name)));
    using var ownership = new Mutex(true, mutexName, out bool firstOwner);
    if (!firstOwner) throw new InvalidOperationException("Another bridge already owns this MIDI destination.");
    if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 26200))
        throw new InvalidOperationException("The preview MIDI API requires Windows 11 25H2 or newer.");
    if (!MidiApi.EnsureServiceAvailable() || !MidiLoopbackManager.IsTransportAvailable)
        throw new InvalidOperationException("Windows MIDI loopback service is unavailable.");
    string id = Guid.NewGuid().ToString("N");
    var response = MidiLoopbackManager.CreateTransientLoopback(new MidiLoopbackCreationConfig(
        new MidiLoopbackEndpointDefinition { Name = name, UniqueId = id },
        new MidiLoopbackEndpointDefinition {
            Name = name + " Bridge", UniqueId = id, CreateOnlyUmpEndpoint = true
        }));
    if (!response.Success)
        throw new InvalidOperationException($"Could not create the MIDI connection: {response.ErrorCode}");
    Guid association = response.CreatedLoopbackEntry.AssociationId;
    try
    {
        using var session = MidiSession.Create("PartyKeys Synthesia bridge");
        if (session == null) throw new InvalidOperationException("Could not create a MIDI session.");
        var connection = session.CreateEndpointConnection(response.CreatedLoopbackEntry.EndpointB.EndpointDeviceId);
        if (connection == null) throw new InvalidOperationException("Could not create the internal MIDI connection.");
        connection.MessageReceived += (_, received) => {
            try
            {
                uint word = received.PeekFirstWord();
                uint type = word >> 28;
                if (((word >> 24) & 15) != 0 || (type != 1 && type != 2 && type != 4)) return;
                var bytes = MidiMessageConverter.ConvertSingleGroupCompleteMessageUmpWordsToMidi1Bytes(
                    received.GetMessagePacket().GetAllWords());
                if (bytes.Count > 0)
                    Output.Report(new { status = "midi", data = bytes.Select(b => (int)b).ToArray() });
            }
            catch (Exception error)
            {
                Output.Report(new { status = "error", message = "MIDI reception failed: " + error.Message });
            }
        };
        if (!connection.Open()) throw new InvalidOperationException("Could not open the internal MIDI connection.");
        Output.Report(new { status = "ready", send = name, mode = "private-loopback",
                            systemApi = MidiApi.IsSystemProvided() });
        using var stopped = new ManualResetEventSlim();
        Console.CancelKeyPress += (_, e) => { e.Cancel = true; stopped.Set(); };
        new Thread(() => {
            string? line;
            while ((line = Console.ReadLine()) != null && line != "quit") { }
            stopped.Set();
        }) { IsBackground = true }.Start();
        stopped.Wait();
        // Close clients before removing their endpoints.
        session.DisconnectEndpointConnection(connection.ConnectionId);
    }
    finally
    {
        var removed = MidiLoopbackManager.RemoveTransientLoopback(new MidiLoopbackRemovalConfig(association));
        Output.Report(new { status = "removed", success = removed.Success });
    }
    return 0;
}
catch (Exception error)
{
    Output.Report(new { status = "error", message = error.Message, hresult = $"0x{error.HResult:X8}" });
    return 1;
}

static class Output
{
    private static readonly object Gate = new();
    public static void Report(object message)
    {
        lock (Gate) Console.WriteLine(JsonSerializer.Serialize(message));
    }
}
