/*  Title:      Isabelle-MCP/scala/src/mcp_main.scala

    Isabelle tool wrapper for "isabelle mcp_server": the PIDE language server
    driven by Isabelle-MCP.  Forked from Tools/VSCode/src/vscode_main.scala.

    The Scala-side PIDE control requests (theory_status, cancel_execution,
    command_at_position, output_at_position, symbols, find_theorems) and the
    caret-perspective EOF clamp live in this component, so the distribution needs
    no Scala patch.  The ML side still does: "Document.cancel_execution" comes
    from the pide_control patch (execution.ML + protocol.ML).
*/

package isabelle.mcp

import isabelle._

import java.io.{PrintStream, OutputStream}

import scala.collection.mutable


object MCP_Main {
  /* component resources */

  def home: Path = Path.explode("$ISABELLE_MCP_SCALA_HOME")

  /* ML injected into the prover before its protocol loop starts (EXPERIMENT) */
  def prelude_ml: Path = home + Path.explode("ML/mcp_prelude.ML")


  /* Isabelle tool wrapper */

  val isabelle_tool =
    Isabelle_Tool("mcp_server", "PIDE language server for Isabelle-MCP", Scala_Project.here,
      { args =>
        try {
          var logic_ancestor: Option[String] = None
          var log_file: Option[Path] = None
          var logic_requirements = false
          val dirs = new mutable.ListBuffer[Path]
          val include_sessions = new mutable.ListBuffer[String]
          var logic = Isabelle_System.default_logic()
          var modes: List[String] = Nil
          var no_build = false
          var options = Options.init()
          var verbose = false

          val getopts = Getopts("""
Usage: isabelle mcp_server [OPTIONS]

  Options are:
    -A NAME      ancestor session for option -R (default: parent)
    -L FILE      logging on FILE
    -R NAME      build image with requirements from other sessions
    -d DIR       include session directory
    -i NAME      include session in name-space of theories
    -l NAME      logic session name (default ISABELLE_LOGIC=""" +
            quote(Isabelle_System.default_logic()) + """)
    -m MODE      add print mode for output
    -n           no build of session image on startup
    -o OPTION    override Isabelle system OPTION (via NAME=VAL or NAME)
    -v           verbose logging

  Run the PIDE language server (LSP over stdin/stdout) for Isabelle-MCP.
""",
            "A:" -> (arg => logic_ancestor = Some(arg)),
            "L:" -> (arg => log_file = Some(Path.explode(File.standard_path(arg)))),
            "R:" -> (arg => { logic = arg; logic_requirements = true }),
            "d:" -> (arg => dirs += Path.explode(File.standard_path(arg))),
            "i:" -> (arg => include_sessions += arg),
            "l:" -> (arg => logic = arg),
            "m:" -> (arg => modes = arg :: modes),
            "n" -> (_ => no_build = true),
            "o:" -> (arg => options = options + arg),
            "v" -> (_ => verbose = true))

          val more_args = getopts(args)
          if (more_args.nonEmpty) getopts.usage()

          val log = Logger.make_file(log_file)
          val channel = new Channel(System.in, System.out, log, verbose)
          val server =
            new Language_Server(channel, options, session_name = logic, session_dirs = dirs.toList,
              include_sessions = include_sessions.toList, session_ancestor = logic_ancestor,
              session_requirements = logic_requirements, session_no_build = no_build,
              modes = modes, log = log)

          // prevent spurious garbage on the main protocol channel
          val orig_out = System.out
          try {
            System.setOut(new PrintStream(OutputStream.nullOutputStream()))
            server.start()
          }
          finally { System.setOut(orig_out) }
        }
        catch {
          case exn: Throwable =>
            val channel = new Channel(System.in, System.out, new Logger)
            channel.error_message(Exn.message(exn))
            throw exn
        }
      })
}

class Tools extends Isabelle_Scala_Tools(MCP_Main.isabelle_tool)
