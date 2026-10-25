// Export selected analyzed functions and their direct references for offline review.
// @category N1X
import ghidra.app.script.GhidraScript;
import ghidra.app.decompiler.DecompInterface;
import ghidra.program.model.address.Address;
import ghidra.program.model.listing.Function;
import ghidra.program.model.symbol.Reference;
import java.io.PrintWriter;
import java.nio.file.Files;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;

public class ExportFunctions extends GhidraScript {
    public void run() throws Exception {
        String[] args = getScriptArgs();
        if (args.length < 2) throw new IllegalArgumentException("OUTPUT ADDRESS [ADDRESS ...]");
        DecompInterface decompiler = new DecompInterface();
        try {
            if (!decompiler.openProgram(currentProgram))
                throw new IllegalStateException("Cannot open program in decompiler");
            try (PrintWriter out = new PrintWriter(Files.newBufferedWriter(
                    Path.of(args[0]), StandardOpenOption.CREATE_NEW))) {
                out.println("Program: " + currentProgram.getName());
                out.println("SHA256: " + currentProgram.getExecutableSHA256());
                for (int index = 1; index < args.length; ++index) {
                    Address address = toAddr(args[index]);
                    Function function = getFunctionContaining(address);
                    if (function == null) {
                        // Supports a targeted -noanalysis import of the large KMD.
                        // Only use established function-entry addresses in this mode.
                        disassemble(address);
                        function = createFunction(address, null);
                    }
                    if (function == null) throw new IllegalArgumentException("No function at " + address);
                    out.println("\n=== " + function.getName() + " " + function.getEntryPoint() + " ===");
                    for (Reference reference : getReferencesTo(function.getEntryPoint()))
                        out.println("Referenced from " + reference.getFromAddress() + " " + reference.getReferenceType());
                    var result = decompiler.decompileFunction(function, 60, monitor);
                    if (!result.decompileCompleted()) throw new IllegalStateException(result.getErrorMessage());
                    out.println(result.getDecompiledFunction().getC());
                }
                if (out.checkError()) throw new IllegalStateException("Writing export failed");
            }
        } finally {
            decompiler.dispose();
        }
    }
}
